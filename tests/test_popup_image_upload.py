"""弹窗主图上传与公开访问测试（user_server /ops/upload_image + /popup_images/<name>）。

覆盖：
  - save_popup_image：魔数校验（png/jpg/webp）、大小上限、内容哈希命名幂等
  - handle_ops_upload_image_http：admin 门禁、缺文件/非法内容 400、成功返回公开 URL
  - handle_popup_image_http：文件名白名单（防路径穿越）、content-type 与长缓存头
"""
import io
import json
import os
import re

import pytest

import user_server
from config import Config
from user_server import UserServer, save_popup_image

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF" + b"\x20\x00\x00\x00" + b"WEBP" + b"\x00" * 16
RIFF_NOT_WEBP = b"RIFF" + b"\x20\x00\x00\x00" + b"WAVE" + b"\x00" * 16


@pytest.fixture(autouse=True)
def _image_dir(tmp_path, monkeypatch):
  """上传目录隔离到 tmp_path。"""
  monkeypatch.setattr(user_server, "run_dir", str(tmp_path))
  return tmp_path


# -------------------- save_popup_image --------------------

class TestSavePopupImage:
  def test_png_saved_with_hash_name_and_public_url(self, _image_dir):
    url, err = save_popup_image(PNG)
    assert err is None
    assert re.fullmatch(
      rf"{re.escape(Config.PUBLIC_API_BASE)}/popup_images/[a-f0-9]{{16}}\.png", url,
    )
    name = url.rsplit("/", 1)[1]
    assert (_image_dir / "data" / "popup_images" / name).read_bytes() == PNG

  def test_jpg_and_webp_extensions(self):
    url, err = save_popup_image(JPG)
    assert err is None and url.endswith(".jpg")
    url, err = save_popup_image(WEBP)
    assert err is None and url.endswith(".webp")

  def test_riff_but_not_webp_rejected(self):
    url, err = save_popup_image(RIFF_NOT_WEBP)
    assert url is None and "png" in err

  def test_same_content_same_name_idempotent(self):
    url1, _ = save_popup_image(PNG)
    url2, _ = save_popup_image(PNG)
    assert url1 == url2  # 内容寻址：重复上传不产生新文件

  def test_empty_and_oversize_and_non_image(self, monkeypatch):
    assert save_popup_image(b"")[1] == "空文件"

    url, err = save_popup_image(b"<html>not an image</html>")
    assert url is None and "仅支持" in err

    monkeypatch.setattr(user_server, "POPUP_IMAGE_MAX_BYTES", 8)
    url, err = save_popup_image(PNG)
    assert url is None and "上限" in err


# -------------------- /ops/upload_image --------------------

class _FileField:
  def __init__(self, filename: str, data: bytes):
    self.filename = filename
    self.file = io.BytesIO(data)


class _UploadReq:
  def __init__(self, fields: dict):
    self._fields = fields
    self.headers = {}

  async def post(self):
    return self._fields


def _server(is_admin: bool):
  server = UserServer.__new__(UserServer)

  async def _check(_tok):
    return "ops-uid" if is_admin else None

  server._check_ops_admin = _check
  return server


class TestUploadHandler:
  async def test_admin_upload_success(self):
    server = _server(True)
    req = _UploadReq({"jwt_token": "tok", "image": _FileField("banner.png", PNG)})
    resp = await UserServer.handle_ops_upload_image_http(server, req)
    assert resp.status == 200
    url = json.loads(resp.text)["data"]["url"]
    assert url.startswith(f"{Config.PUBLIC_API_BASE}/popup_images/")

  async def test_forbidden_for_non_admin(self):
    server = _server(False)
    req = _UploadReq({"jwt_token": "tok", "image": _FileField("banner.png", PNG)})
    resp = await UserServer.handle_ops_upload_image_http(server, req)
    assert resp.status == 403

  async def test_missing_file_400(self):
    server = _server(True)
    resp = await UserServer.handle_ops_upload_image_http(server, _UploadReq({"jwt_token": "tok"}))
    assert resp.status == 400
    # 只有文件名空串（浏览器未选文件）也视为缺文件
    req = _UploadReq({"jwt_token": "tok", "image": _FileField("", PNG)})
    resp = await UserServer.handle_ops_upload_image_http(server, req)
    assert resp.status == 400

  async def test_invalid_content_400(self):
    server = _server(True)
    req = _UploadReq({"jwt_token": "tok", "image": _FileField("x.png", b"nope")})
    resp = await UserServer.handle_ops_upload_image_http(server, req)
    assert resp.status == 400


# -------------------- /popup_images/<name> --------------------

class _GetReq:
  def __init__(self, name: str):
    self.match_info = {"name": name}


class TestServeHandler:
  async def test_serves_image_with_cache_headers(self):
    url, _ = save_popup_image(PNG)
    name = url.rsplit("/", 1)[1]
    resp = await UserServer.handle_popup_image_http(UserServer.__new__(UserServer), _GetReq(name))
    assert resp.status == 200
    assert resp.content_type == "image/png"
    assert resp.body == PNG
    assert "immutable" in resp.headers["Cache-Control"]

  async def test_rejects_traversal_and_unknown_names(self):
    server = UserServer.__new__(UserServer)
    for bad in ("../config.py", "..%2f..%2fconfig.py", "x" * 16 + ".png", "a" * 16 + ".gif", ""):
      resp = await UserServer.handle_popup_image_http(server, _GetReq(bad))
      assert resp.status == 404, bad

  async def test_missing_file_404(self):
    server = UserServer.__new__(UserServer)
    resp = await UserServer.handle_popup_image_http(server, _GetReq("0" * 16 + ".png"))
    assert resp.status == 404
