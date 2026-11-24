"""会员查询必须区分真实 free 与查询失败，后者不得触发计划同步。"""
import time
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import user_server as us
from tests.test_sleep_plan_sync import _sync_body, make_plan, _build_sleep_plan_app
from user_profile import UserProfile


@pytest.fixture(autouse=True)
def enable_membership_check(monkeypatch):
  # 本文件原有用例验证真实会员模式；放行模式的用例单独关闭开关。
  monkeypatch.setattr(us.Config, 'SLEEP_PLAN_CHECK_MEMBERSHIP', True)


def make_server():
  return SimpleNamespace(_tier_cache={}, debug_uid_set=set())


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["free", "pro", "premium"])
async def test_valid_rights_cached(monkeypatch, level):
  calls = []

  async def rights(request):
    calls.append(await request.json())
    return web.json_response({"code": 0, "data": {"effective_user_level": level}})

  app = web.Application()
  app.router.add_post('/auth', rights)
  async with TestServer(app) as auth:
    monkeypatch.setattr(us.Config, "AUTH_SERVER_URL", str(auth.make_url('')).rstrip('/'))
    srv = make_server()
    for _ in range(2):
      assert await us.UserServer._get_effective_level(srv, 'uid', 'test-token') == level
  assert len(calls) == 1
  assert calls[0]['request_type'] == 'query_user_rights'
  assert calls[0]['data']['jwt_token'] == 'test-token'


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [
  (503, {"code": 0, "data": {"effective_user_level": "premium"}}),
  (401, {"detail": "Unauthorized"}),
  (200, {"code": 500, "data": {"effective_user_level": "premium"}}),
  (200, {"code": 0, "data": {}}),
  (200, {"code": 0, "data": {"effective_user_level": "basic"}}),
  (200, {"code": 0, "data": {"effective_user_level": None}}),
  (200, {"code": 0, "data": []}),
  (200, []),
  (200, "invalid JSON"),
])
async def test_lookup_failure_not_cached_and_recovers(monkeypatch, status, body):
  calls = []

  async def rights(request):
    calls.append(1)
    if len(calls) == 1:
      if isinstance(body, str):
        return web.Response(text=body, status=status)
      return web.json_response(body, status=status)
    return web.json_response({"code": 0, "data": {"effective_user_level": "premium"}})

  app = web.Application()
  app.router.add_post('/auth', rights)
  async with TestServer(app) as auth:
    monkeypatch.setattr(us.Config, "AUTH_SERVER_URL", str(auth.make_url('')).rstrip('/'))
    srv = make_server()
    # An expired successful cache must not hide the upstream failure.
    srv._tier_cache['uid'] = ('free', time.time() - us.Config.SLEEP_PLAN_TIER_CACHE_SECONDS - 1)
    with pytest.raises(us.MembershipLookupError):
      await us.UserServer._get_effective_level(srv, 'uid', 'test-token')
    assert await us.UserServer._get_effective_level(srv, 'uid', 'test-token') == 'premium'
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_timeout_not_cached(monkeypatch):
  session = Mock(side_effect=TimeoutError)
  monkeypatch.setattr(us, 'ClientSession', session)
  srv = make_server()
  with pytest.raises(us.MembershipLookupError):
    await us.UserServer._get_effective_level(srv, 'uid', 'test-token')
  assert srv._tier_cache == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('request_type', ['sync_plans', 'query_plans'])
async def test_unavailable_rights_does_not_read_or_write_profile(request_type, caplog):
  caplog.set_level(logging.INFO)
  async def unavailable(*args):
    raise us.MembershipLookupError('unavailable')

  srv = SimpleNamespace(
    _parse_for_uid=lambda data: 'uid',
    _get_effective_level=unavailable,
    user_serv=Mock(),
  )

  async def handler(request):
    return await us.UserServer.handle_sleep_plan_http(srv, request)

  app = web.Application()
  app.router.add_post('/sleep_plan', handler)
  async with TestClient(TestServer(app)) as client:
    body = _sync_body(request_type=request_type, plans=[make_plan().model_dump(mode='json')])
    response = await client.post('/sleep_plan', json=body)
    assert response.status == 503
    result = await response.json()
    assert result['code'] == 503
    assert 'data' not in result
  srv.user_serv.get_profile.assert_not_called()
  srv.user_serv.save_profile.assert_not_called()
  assert 'sleep_plan membership unavailable:' in caplog.text
  assert 'status=503' in caplog.text


@pytest.mark.asyncio
async def test_bypass_skips_auth_and_cached_free(monkeypatch):
  monkeypatch.setattr(us.Config, 'SLEEP_PLAN_CHECK_MEMBERSHIP', False)
  session = Mock(side_effect=AssertionError('must not query auth'))
  monkeypatch.setattr(us, 'ClientSession', session)
  srv = make_server()
  srv._tier_cache['uid'] = ('free', time.time())
  original_cache = dict(srv._tier_cache)
  assert await us.UserServer._get_effective_level(srv, 'uid', 'test-token') == 'premium'
  assert srv._tier_cache == original_cache
  session.assert_not_called()
  # 放行结果不能缓存为真实会员等级；重新开启仍走真实等级。
  monkeypatch.setattr(us.Config, 'SLEEP_PLAN_CHECK_MEMBERSHIP', True)
  assert await us.UserServer._get_effective_level(srv, 'uid', 'test-token') == 'free'


@pytest.mark.asyncio
async def test_bypass_sync_and_query_persist_plans_and_log(monkeypatch, caplog):
  monkeypatch.setattr(us.Config, 'SLEEP_PLAN_CHECK_MEMBERSHIP', False)
  session = Mock(side_effect=AssertionError('must not query auth'))
  monkeypatch.setattr(us, 'ClientSession', session)
  caplog.set_level(logging.INFO)
  profile = UserProfile()
  app = _build_sleep_plan_app(profile=profile, tier=None)
  plans = [make_plan(plan_id, custom_name='私密计划名称').model_dump(mode='json') for plan_id in [
    '58DA0CAF-79F3-46DB-B3F6-BD01AD2DB73C',
    'C8C4AC40-8209-458D-B4D3-1998787595A4',
  ]]
  async with TestClient(TestServer(app)) as client:
    for request_type in ['sync_plans', 'query_plans']:
      response = await client.post('/sleep_plan', json=_sync_body(
        request_type=request_type, jwt_token='secret-test-token',
        plans=plans if request_type == 'sync_plans' else None,
      ), headers={'X-Client-Request-ID': 'client-sync-test'})
      assert response.status == 200
      data = (await response.json())['data']
      assert data['membership_tier'] == 'premium'
      assert data['quota'] == {'used': 2, 'limit': 5}
      assert {p['plan_id'] for p in data['plans']} == {p['plan_id'] for p in plans}
      assert data['rejected'] == []
  assert len(app.saved['jwt_uid'].sleep_plans) == 2
  session.assert_not_called()
  for marker in ['sleep_plan request:', 'sleep_plan result:', 'sleep_plan finished:',
                 'sync_plans', 'query_plans', 'jwt_uid', 'status=200', 'elapsed_ms=',
                 '"membership_check": false', '"limit": 5', '"rejected": []',
                 'client_request_id=client-sync-test']:
    assert marker in caplog.text
  assert 'secret-test-token' not in caplog.text
  assert '私密计划名称' not in caplog.text
  # 两次请求各自具备一套完整的可关联日志。
  messages = [r.getMessage() for r in caplog.records if r.getMessage().startswith('sleep_plan ')]
  ids = [m.split('request_id=')[1].split()[0] for m in messages]
  assert len(set(ids)) == 2
  assert all(ids.count(request_id) == 5 for request_id in set(ids))


@pytest.mark.asyncio
async def test_bypass_keeps_five_plan_limit(monkeypatch, caplog):
  monkeypatch.setattr(us.Config, 'SLEEP_PLAN_CHECK_MEMBERSHIP', False)
  caplog.set_level(logging.INFO)
  app = _build_sleep_plan_app(profile=UserProfile(), tier=None)
  async with TestClient(TestServer(app)) as client:
    response = await client.post('/sleep_plan', json=_sync_body(
      plans=[make_plan(f'PLAN-{i}').model_dump(mode='json') for i in range(6)],
    ))
    data = (await response.json())['data']
    assert data['quota'] == {'used': 5, 'limit': 5}
    assert data['rejected'] == [{'plan_id': 'PLAN-5', 'reason': 'quota_exceeded'}]
  assert 'quota_exceeded' in caplog.text
  assert 'PLAN-5' in caplog.text


@pytest.mark.asyncio
async def test_enabled_check_keeps_free_rejection_log(caplog):
  caplog.set_level(logging.INFO)
  app = _build_sleep_plan_app(profile=UserProfile(), tier='free')
  async with TestClient(TestServer(app)) as client:
    response = await client.post('/sleep_plan', json=_sync_body(
      plans=[make_plan().model_dump(mode='json')],
    ))
    data = (await response.json())['data']
    assert data['quota'] == {'used': 0, 'limit': 0}
    assert data['rejected'][0]['reason'] == 'tier_not_allowed'
  assert 'tier_not_allowed' in caplog.text
  assert '"membership_check": true' in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_kind,status', [('auth', 401), ('schema', 400), ('json', 400)])
async def test_bypass_keeps_auth_and_validation_logs(monkeypatch, caplog, invalid_kind, status):
  monkeypatch.setattr(us.Config, 'SLEEP_PLAN_CHECK_MEMBERSHIP', False)
  caplog.set_level(logging.INFO)
  app = _build_sleep_plan_app(profile=UserProfile(), tier=None)
  async with TestClient(TestServer(app)) as client:
    if invalid_kind == 'json':
      response = await client.post('/sleep_plan', data='{')
    else:
      data = _sync_body()
      data['data'] = {'uid': 'unauthorized'} if invalid_kind == 'auth' else {
        'jwt_token': 'secret-validation-token', 'plans': 'invalid-list',
      }
      response = await client.post('/sleep_plan', json=data)
    assert response.status == status
  assert app.saved == {}
  assert f'status={status}' in caplog.text
  assert 'sleep_plan received:' in caplog.text
  assert 'secret-validation-token' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('token_error', [us.jwt.InvalidTokenError, us.jwt.ExpiredSignatureError])
async def test_bypass_retains_real_login_authentication(monkeypatch, caplog, token_error):
  monkeypatch.setattr(us.Config, 'SLEEP_PLAN_CHECK_MEMBERSHIP', False)
  verifier = Mock(side_effect=token_error)
  monkeypatch.setattr(us, 'verify_token', verifier)
  caplog.set_level(logging.INFO)
  # 使用真实的 handler -> _parse_for_uid -> _check_token 鉴权链。
  srv = object.__new__(us.UserServer)
  srv.user_serv = Mock()
  srv.debug_uid_set = set()
  srv._tier_cache = {}
  app = web.Application()
  app.router.add_post('/sleep_plan', srv.handle_sleep_plan_http)
  async with TestClient(TestServer(app)) as client:
    response = await client.post('/sleep_plan', json=_sync_body(
      jwt_token='invalid-or-expired-token', plans=[make_plan().model_dump(mode='json')],
    ))
    assert response.status == 401
  verifier.assert_called_once_with('invalid-or-expired-token')
  srv.user_serv.get_profile.assert_not_called()
  srv.user_serv.save_profile.assert_not_called()
  assert 'invalid-or-expired-token' not in caplog.text
  assert 'status=401' in caplog.text
