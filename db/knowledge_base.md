# Sleep Intervention Knowledge Base
## all lights candidates
### L1
- desc: 日落光 + 熄灯过渡
### L2
- desc: 微红光

## all spray candidates
### S1
- desc: 薰衣草 + 雪松, GABA增强,睡前30分钟
### S2
- desc: 薰衣草 + 洋甘菊,持续扩散 脉冲

## insomnia.onset
### mechanism
- 高认知唤醒
- 褪黑素延迟

### condition:
- sleep_latency > 30
- freq(SL>30) > 0.5

### light
- type: 日落光 + 熄灯过渡
- lux: <10
- note: 避免蓝光

### audio
- type: 粉红噪音 / 自然音
- bpm: 55-65
- structure: 无歌词、无突变

### aroma
- formula: 薰衣草 + 雪松
- mechanism: GABA增强
- timing: 睡前30分钟

---

## insomnia.maintenance

### condition:
- WASO > 30
- awakenings >= 2

### mechanism
- 睡眠浅 / 易觉醒

### light
- type: 微红光
- lux: <10

### audio
- type: 棕色噪音 / 诵经
- duration: 90min

### aroma
- formula: 薰衣草 + 洋甘菊
- strategy: 持续扩散 + 脉冲

---

## insomnia.terminal
### condition:
- early_wake > 30

### mechanism
- 相位前移 + 抑郁相关

### light
- strategy: 完全黑暗 + 日出模拟

### audio
- type: 情绪稳定音乐

### aroma
- formula: 薰衣草 + 佛手柑
- timing: 凌晨4点


## SpecialGroups.BIC
### condition:
- elderly: age < 13
### mechanism
- 就寝抵抗 + 频繁夜醒

## SpecialGroups.old
### condition:
- elderly: age >= 65
### mechanism
- 相位前移 + 抑郁相关 + 浅睡增多 

## SpecialGroups.Perimenopause
### condition:
- perimenopause: female && 45-55
### mechanism
- 潮热盗汗 + 睡眠维持障碍

## SpecialGroups.PTSD
### mechanism
- 噩梦 + 过度警觉 + 入睡恐惧
### condition:
- anxiety: GAD7 > threshold