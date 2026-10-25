# Web2API Studio

本地图片和视频生成网关，优先使用 Gemini 网页账号 1 和账号 2；两个账号都明确额度耗尽时，可以自动切换到付费的火山方舟 Seedream 5.0 Flash。另提供 Google Labs ImageFX、Bing Image Creator、Recraft Free plan、Leonardo Free plan、即梦、通义万相和 Hailuo 等网页连接器：填写用户自己的 Cookie / Token 后由本机请求第一方网页接口。即梦只使用网页赠送积分，通义万相只使用 `bonus` 奖励灵感值；其他平台会在界面标为候选或需要实测，不把网页入口当成固定免费额度。服务不自动登录、不处理 CAPTCHA/WAF，也不会把官方 API 额度伪装成网页免费额度。支持参考图编辑、批量任务、Gemini 文字对话和 HTTP API。Gemini 连接依赖 [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API)（`gemini-webapi==2.1.1`）。

Seedream 是付费兜底，只有 Gemini 双账号明确返回额度耗尽时才会发起；认证失败、网络异常、内容拒绝等错误不会触发付费调用。火山方舟当前公开的 Seedream 5.0 Flash 输出单价为 ¥0.12/张，按成功生成的图片张数计费。任务历史会保存上游用量并显示费用估算；最终金额可能受账户折扣、资源包等影响，以火山方舟账单为准。自定义网站是配置能力，并不表示所有模型网站已经逐一验证。

## 免费网页额度与 Q3 / Q2 替代品

### 状态口径

页面和接口现在会给每个平台返回 `verification_status`、`verification_label`、`free_bucket` 和 `browser_required`。这些字段用于区分协议已经写好、账号免费桶是否明确，以及是否可能需要回网页完成验证：

| 状态 | 含义 |
|---|---|
| `verified_web` | 本项目已经用真实网页账号跑通过该网页入口；仍以账号当日额度为准 |
| `web_quota_declared` | 网页明确返回独立免费桶，代码会在提交前检查；当前需要你用自己的账号实测 |
| `web_candidate` | 有网页免费入口和适配器，但尚未完成真实账号端到端验证；不保证能用 |
| `browser_challenge` | 可能要求 CAPTCHA、AWS WAF、动态设备签名或网页验证；本地服务不会绕过挑战 |
| `api_only` | 只能按官方 API Key 和控制台额度调用，网页赠送积分不适用 |

因此，Q3/Q2 的“替代”是能力定位，不等于固定免费次数。当前最可靠的免费路径仍是 Gemini 双账号；视频优先试即梦、通义万相和 Vidu 网页的免费桶，其他网页平台先看卡片状态和余额再提交。Vidu 网页与 Vidu 官方 API 是两个独立连接器，网页 Cookie 的免费 credits 不会被伪装成 API 额度。

Qwen 网页目前只列为候选：网页有文生图、图生图和文生视频入口，但直接 `POST /api/v2/chats/new` 和 `POST /api/v2/entitlement/credits/verify` 都需要登录 access token；网页还会加载 Baxia 风控模块。2026-10-25 的无凭据探测中，`GET /api/v2/models/` 能列出带 `t2i` 能力的模型，`GET /api/v2/configs/` 只返回会员/credits 功能开关，没有可确认的免费余额字段，两个需要登录的接口均返回 401。因此当前不能把 Qwen 宣称为稳定免费 Web2API，也不会在额度未知时提交任务。服务不会自动生成设备指纹、计算 PoW 或绕过 AWS WAF；只有后续能用你从正常网页复制的合法请求头稳定调用，并能确认独立免费桶时，才会作为连接器接入。

优先接入能用网页账号 session 直连、并且官方页面明确展示免费积分的服务。网页积分、会员积分、购买积分和官方 API 额度是不同的余额；本项目不会把网页免费说明误写成 API 免费额度。

| 平台 | 定位 | 当前接入 | 免费依据与限制 |
|---|---|---|---|
| Gemini Apps | 图片和文字基准 | Gemini 双账号 Cookie；生图、图生图、文字 | [官方帮助](https://support.google.com/gemini/answer/14286560?hl=en)说明图片模型有 daily quota；每次新请求都会重新探测账号 |
| Google Labs ImageFX | Q3 / Q2 图片替代 | **适配器已接入 · 候选**；ImageFX / Imagen 网页文生图，Cookie 换短期 session token，不需要常驻浏览器 | [Google Labs ImageFX](https://labs.google/fx/tools/image-fx)是消费网页入口；免费额度、地区和模型权限由 Google 账号页面决定，Google 没有为该网页承诺固定每日 API 数量。此连接只使用用户自己的网页会话，不是 Gemini API Key |
| Bing Image Creator | Q3 / Q2 图片替代 | **适配器已接入 · 候选**；Bing Image Creator 网页文生图，支持 DALL·E 3、GPT-4o、MAI Image | [Bing Image Creator](https://www.bing.com/images/create)使用微软账号自己的免费 Boost / 网页额度；速度和可用模型由账号、地区和页面状态决定，Boost 用尽后不宣称仍然免费 |
| Recraft 网页 Free plan | Q3 / Q2 图片替代 | **适配器已接入 · 候选**；Recraft 网页文生图，默认 `gpt_image_2p5_flare`，不需要常驻浏览器 | [Recraft Free plan](https://www.recraft.ai/) 的 `plan.is_free` 和 `subscription_credits` 会在每次生成前校验；只使用可确认的免费订阅 credits，Free plan 图片公开展示且按 Recraft 规则限制商用 |
| Leonardo 网页 Free plan | Q3 / Q2 图片替代 | **适配器已接入 · 候选**；Leonardo 网页文生图，不需要常驻浏览器 | [Leonardo](https://app.leonardo.ai/image-generation) 每次生成前核验 Free plan 和 `subscriptionTokens`；购买 Token、API credits 和非 Free plan 账户不会被使用；当前不接参考图 |
| 即梦 AI 网页 | Q3 / Q2 图片与视频替代 | **免费桶字段明确 · 需账号实测**；Seedream 5.0 Flash / Lite / 4.7 / 4.0 / 3.1；Seedance、HappyHorse、Wan；文生、参考图、首尾帧 | [官方网页](https://jimeng.jianying.com/)明确列出“平台免费积分：每日登录赠送积分”；只要 `gift_credit` 大于 0 就提交，购买/会员积分不会被主动使用，赠送积分为 0 时停止 |
| 通义万相网页 | Q3 / Q2 图片与视频替代 | **免费桶字段明确 · 需账号实测**；Wan 2.7 / 2.6 / 2.5 / 2.2 / 2.1 / 3.0，文生图、参考图生图、文生视频、首尾帧 | [网页一方接口](https://tongyi.aliyun.com/wanxiang/)返回 `bonus`、`member`、`topUp` 三类灵感值；只使用 `bonus`，可自动尝试领取每日登录奖励，会员/充值桶不足以确认免费时不会提交 |
| 豆包网页 | Q3 / Q2 图片与视频替代候选 | **实验性直连 · 候选**；网页对话、Seedance 2.0 文生视频和文生图；当前不接参考图 | [豆包网页](https://www.doubao.com/chat/)使用网页登录/赠送额度，但上游没有稳定公开免费桶分类。需要 Cookie、`device_id`、`web_id` 等用户自己的上下文；服务不生成设备指纹、不实现 WebMSSDK/X-Bogus、不处理 CAPTCHA，遇到验证会停止 |
| Hailuo / MiniMax 网页 | Q3 / Q2 图片与视频替代 | **适配器已接入 · 候选**；Hailuo 网页图片模型、Hailuo 3.0 / Seedance 2.0 视频；文生图、最多 1 张参考图、文生视频、首尾帧 | [Hailuo](https://hailuoai.video)的余额接口按购买、订阅赠送和登录赠送分类；只使用登录/订阅赠送桶，无法确认分类或免费桶为 0 时不会提交 |
| Vidu 网页 | Vidu Q3 / Q2 视频 | **网页适配器已接入 · 免费桶需账号实测**；Q2/Q3 文生、单图、首尾帧和最多 7 张主体参考图 | [官方网页](https://www.vidu.cn/)的 Cookie 直接请求网页任务服务；提交前读取网页额度，无法确认免费桶或余额为 0 时不提交。若账号触发登录、风控或验证，需要先在网页完成验证后更新 Cookie |

适配器支持“填字段后直接调用”，但是否能用取决于你账号当前状态。凭据只保存在本机，固定发往各自第一方域名；服务不自动登录、不处理 CAPTCHA/WAF。即梦和通义万相有明确的免费桶检查；ImageFX、Bing、Recraft、Leonardo、Hailuo、PixVerse 仍应按卡片上的候选状态先查询或实测，不能把存在网页入口理解成固定免费次数。Hailuo 图片接口支持最多 1 张参考图，需要更多参考图时继续使用 Gemini、即梦或通义万相。Vidu、PixVerse 和 Kling 的网页挑战不会被伪装成免费 API。

### 当前免费网页入口与未接入候选

| 结论 | 平台 | 原因 |
|---|---|---|
| 适配器已实现 | Gemini Apps、Google Labs ImageFX、Bing Image Creator、Recraft、Leonardo、即梦 AI、通义万相、Hailuo、豆包网页 | 有网页入口，能在不绕过登录/挑战的前提下使用用户提供的 Cookie 或网页 Token；是否有可用免费桶由每个平台状态字段和账号实测决定。豆包实验性直连要求用户提供网页上下文，遇到验证会停止 |
| 已保留为网页候选 | Qwen 网页、Whisk、Ideogram、SeaArt、Freepik | 这些产品存在免费体验、积分或每日奖励的公开入口，但额度、地区或内部接口经常变化；Qwen 的图片/视频请求通常还需要网页风控字段。没有稳定的第一方字段协议前，不把它们标成“已接入免费 API”，也不会自动生成或绕过风控字段 |
| 不作为免费方案 | Gemini API、火山方舟、MiniMax API、Vidu API、百炼 API | 这些是开发者 API；网页会员/赠送积分不自动转成 API 免费额度 |

手动选择平台时，图片建议先试 Gemini，再试 ImageFX、即梦、通义万相或 Hailuo；视频建议先试即梦，再试通义万相、Hailuo、PixVerse 或 Kling。跨平台不会自动切换；只有 Gemini 账号池按“账号 1 → 账号 2 → Seedream 兜底”自动路由。每个平台只会使用它明确标出的网页赠送桶；没有可确认的免费余额时直接返回额度错误，不会暗扣购买积分。

## 视频网页额度与官方 API

视频工作台同时接入网页账号额度和官方 API。即梦、通义万相、Vidu、Hailuo、PixVerse、Kling 和豆包提供网页连接器；其中 Vidu、Kling 和豆包可能需要在网页先完成登录、验证码或风控验证，其他候选平台也可能因账号、地区或模型权限不可用。网页免费额度和模型权限由账号决定，不能保证每个账号都有免费 credits。官方 API 的额度仍与网页积分分开。凭据只保存在本机私有数据目录，页面不会回显；本地接口不会绕过挑战。

| 平台 | 适合定位 | 当前接入的能力 | 免费说明 | 文档 |
|---|---|---|---|---|
| 即梦 AI 网页 | Q3 / Q2 图片与视频替代；先用每日网页赠分打样 | Seedream 图片、Seedance / HappyHorse / Wan 视频；文生、参考图、首尾帧 | 填 `sessionid`，默认只使用 `gift_credit`；设置页可查询赠送、购买和会员余额 | [即梦](https://jimeng.jianying.com/) |
| 通义万相网页 | Q3 / Q2 图片与视频替代；Wan 2.7/3.0 优先 | Wan 图片、文生视频、首尾帧图生视频；无需常驻浏览器 | 填完整 Cookie Header（含 `XSRF-TOKEN`）；自动查询 `bonus` 并尝试领取每日奖励，`member` / `topUp` 不会被使用 | [通义万相](https://tongyi.aliyun.com/wanxiang/) |
| PixVerse / 拍我 AI 网页 | Q3 / Q2 风格替代候选；原生模型打样 | v6、pixverse-c1、v5.6、v5.5、v5-fast；文生、单张参考图生视频 | 设置页区分每日、月度和套餐余额；生成前只允许明确的每日/月度免费余额，套餐 credits 不会自动消耗 | [拍我 AI](https://pai.video) |
| Hailuo / MiniMax 网页 | Q3 / Q2 网页额度候选 | Hailuo 3.0、Seedance 2.0；文生、参考图、首尾帧 | 设置页区分登录赠送、订阅赠送和购买 credits；生成前只允许登录/订阅赠送余额，无法确认分类或免费余额为 0 时不会提交任务 | [Hailuo](https://hailuoai.video) |
| 豆包网页 | **实验性** Q3 / Q2 网页额度候选；Seedance 2.0 动作和运镜 | 网页对话、文生图、文生视频；参考图需要官方网页签名，当前直连不提交参考图 | 在设置页粘贴 JSON：`{"cookie":"...","device_id":"...","web_id":"...","fp":"...","ms_token":"..."}`。这些值必须来自自己的正常网页会话；不生成设备指纹、不绕过 WebMSSDK/CAPTCHA。网页额度分类不可见，仍需账号实测 | [豆包](https://www.doubao.com/chat/) |
| Kling AI 网页 | **实验性** Q3 / Q2 网页额度候选；动作和运镜 | 网页模型 1.6 / 2.0 / 2.1 / 2.5 / 2.6；文生、首帧、首尾帧 | 填写 `klingai.com` 完整 Cookie Header；可查询网页 points/tickets。当前未完成纯 HTTP 端到端验证；账号触发验证码、登录、风控或动态签名时需先在网页完成验证 | [Kling](https://klingai.com/app/video/new) |
| MiniMax H3 | Q3 风格替代；对白短剧、口型和音画同步 | 文生、首尾帧、最多 9 张参考图、异步轮询 | 新用户网页额度与 API 额度分开；API 体验额度以控制台为准 | [视频生成](https://platform.minimax.cn/docs/guides/video-generation) |
| Vidu Q3 / Q2 | 原味 Q3 / Q2；多主体一致性、音画一体 | Q3、Q2 文生、图生、首尾帧；主体模型最多 7 张图片参考，Q2/Q3 的普通图生和首尾帧按模型最多 2 张；设置页可查询 API credits | 网页积分与 API 分开；API credits 以 Vidu 控制台为准，查询到的是 API 余额，不代表网页免费额度 | [Q3 图生](https://platform.vidu.com/docs/api-reference/video-models/vidu-q3/image-to-video) / [Q3 主体参考](https://platform.vidu.com/docs/api-reference/video-models/vidu-q3/reference-to-video) / [Q2 图生](https://platform.vidu.com/docs/api-reference/video-models/vidu-q2/image-to-video) |
| Vidu 网页 Q3 / Q2 | 直接复用 Vidu 中国网页免费额度 | Q2/Q3 文生、单图、首尾帧和最多 7 张主体参考图；不需要常驻浏览器 | 填完整 Vidu Cookie Header；设置页可查询免费桶和总可用值。免费桶未知或为 0 时不会提交；Cookie 失效时需在网页重新登录并更新 | [Vidu 中国网页](https://www.vidu.cn/) |
| Seedance | Q2 风格替代；动作、运镜、多镜头连续性 | 文生、首帧、首尾帧、参考图；音频取决于模型 | seed.ai 网页免费不自动转为方舟 API 额度；以方舟控制台为准 | [方舟视频任务](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh) |
| 阿里百炼 HappyHorse / Wan | Q2 低成本打样和批量试片 | 首帧图生视频、模型相关的参考图和音频 | 新用户可能有模型体验额度，地域和模型开通状态以控制台为准 | [图生视频](https://www.alibabacloud.com/help/en/model-studio/legacy-image-to-video-api-reference/) |

建议先用即梦或通义万相查询免费桶，再按卡片状态决定是否测试 Hailuo、PixVerse 或 Kling；这些候选平台没有账号端到端验证前不要放进自动批量链路。MiniMax H3 适合对白和音画，Vidu Q3 / Q2 适合多主体与首尾帧，Seedance 适合动作和运镜，百炼适合批量打样，但它们的官方 API 额度与网页免费积分分开。PixVerse 的站内 Seedance、Veo 等外部模型没有列入网页免费候选。

按 Vidu Q3 / Q2 的定位选择网页替代：

- **Q3 方向**：优先试通义万相 `wan3.0`、即梦 Seedance 2.5、Hailuo 3.0、Kling 2.x；它们更适合镜头运动、人物一致性和复杂动作，但免费额度由各网页账号决定。
- **Q2 方向**：优先试通义万相 `wan2.6` / `wan2.5`、即梦 Seedance 2.0 Mini、PixVerse 原生模型；它们更适合低成本批量打样。Vidu Q3/Q2 官方 API、MiniMax API、百炼 API 和方舟 API 的赠送额度不等于网页免费额度，因此仍标为付费 API 连接。

调用免费网页视频入口时，先在网页额度与平台连接中配置自己的账号凭据，然后项目请求明确选择网页 Provider。比如即梦 Seedance 2.5 样片：

```sh
curl --max-time 1200 http://127.0.0.1:8765/v1/video/jobs \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: jimeng-q3-sample-001' \
  -d '{
    "provider": "jimeng-web",
    "model": "jimeng-seedance-2.5-draft",
    "prompt": "一位穿红色外套的登山者沿山脊向前行走，镜头平稳跟拍，远处云层缓慢移动",
    "duration": 5,
    "resolution": "720p",
    "aspect_ratio": "16:9",
    "audio": false
  }'
```

Q2 样片可以把 `model` 改为 `jimeng-seedance-2.0-mini`。请求会在即梦网页 `gift_credit` 足够时提交；凭据无效、免费桶不足或模型权限不符会返回错误，不会自动改用 Vidu API 或其他收费 API。不同平台之间目前由调用方显式选择 `provider`，批量任务也应逐项指定平台。

如果要调用原味 Vidu，则在设置页配置 Vidu 官方 API Key，项目请求使用 `provider: "vidu"`。Q3 可选 `viduq3-turbo`、`viduq3-pro`、`viduq3-pro-fast`、`viduq3` 或 `viduq3-mix`；Q2 可选 `viduq2`、`viduq2-turbo`、`viduq2-pro` 或 `viduq2-pro-fast`。普通文生、单图和首尾帧会按模型选择对应端点；主体模型使用 1–7 张参考图。此路径消耗 Vidu API credits；Vidu 网页注册赠送积分不会被读取或当作 API 免费额度。

Vidu Q3 文生视频示例：

```sh
curl --max-time 1200 http://127.0.0.1:8765/v1/video/jobs \
  -H 'Authorization: Bearer YOUR_PROJECT_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: vidu-q3-001' \
  -d '{
    "provider": "vidu",
    "model": "viduq3-turbo",
    "prompt": "一位登山者沿山脊向前行走，镜头平稳跟拍，云层缓慢移动",
    "duration": 5,
    "resolution": "720p",
    "aspect_ratio": "16:9",
    "audio": true
  }'
```

Q2 图生视频使用同一个地址，先调用 `POST /v1/references` 上传图片，再把返回的 `ref_...` 放入 `reference_images`；例如把 `model` 改为 `viduq2-turbo`，并传两张图时会调用 Q2 首尾帧接口。Vidu Q3/Q2 的 API Key 与网页免费积分是两套凭据和余额，服务不会把网页积分当成免费 API。

如果使用 Vidu 中国网页免费额度，在设置页配置 **Vidu 网页（Q3 / Q2 免费额度）** 的完整 Cookie Header，项目请求把 `provider` 改为 `vidu-web`，模型可选 `vidu-web-q3`、`vidu-web-q3-pro`、`vidu-web-q3-fast`、`vidu-web-q3-lite`、`vidu-web-q2` 或 `vidu-web-q2-pro`。无参考图是文生视频，1 张是单图，2 张是首尾帧，3–7 张会走主体参考；服务每次提交前先查询网页免费桶，余额未知或为 0 时不会提交。浏览器不需要持续打开。

```sh
curl --max-time 1200 http://127.0.0.1:8765/v1/video/jobs \
  -H 'Authorization: Bearer YOUR_PROJECT_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: vidu-web-q3-001' \
  -d '{
    "provider": "vidu-web",
    "model": "vidu-web-q3",
    "prompt": "一位登山者沿山脊向前行走，镜头平稳跟拍，云层缓慢移动",
    "duration": 5,
    "resolution": "720p",
    "aspect_ratio": "16:9",
    "audio": false
  }'
```

### Puter 统一入口

如果你使用 Puter 里的 Vidu Q3 或其他视频模型，在 **Gemini 账号 → 网页额度与平台连接 → Puter AI** 填写自己的 `auth_token_v2`。Puter 连接不要求本机浏览器持续打开，但它按 Puter User-Pays 账户结算，不能视为固定免费额度；页面里的 `test_mode` 样片也不代表正式生成免费。Puter 当前公开目录包含 `vidu/vidu-q3-turbo` 和 `vidu/vidu-q3`，Q2 请使用上面的官方 `vidu` Provider。

Puter Q3 文生视频示例：

```sh
curl --max-time 1200 http://127.0.0.1:8765/v1/video/jobs \
  -H 'Authorization: Bearer YOUR_PROJECT_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: puter-q3-001' \
  -d '{
    "provider": "puter",
    "model": "vidu/vidu-q3-turbo",
    "prompt": "一位登山者沿山脊向前行走，镜头平稳跟拍，云层缓慢移动",
    "duration": 5,
    "resolution": "720P",
    "aspect_ratio": "16:9",
    "audio": true
  }'
```

Puter 图片模型也使用同一个本地网关。比如 Seedream 5.0 Lite：

```sh
curl --max-time 600 http://127.0.0.1:8765/v1/images/generations \
  -H 'Authorization: Bearer YOUR_PROJECT_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: puter-image-001' \
  -d '{
    "provider": "puter",
    "model": "seedream-5-0-lite-260128",
    "prompt": "电影感的雨夜街道，霓虹倒影，写实摄影",
    "n": 1,
    "size": "1024x1024",
    "response_format": "url"
  }'
```

### Pollinations 公共图片入口

`pollinations` 不需要填写 Key，当前只开放公共 `sana` 文生图，可能受到共享限流；它不接受本地参考图。调用方仍需使用本地项目 Key：

```sh
curl --max-time 600 http://127.0.0.1:8765/v1/images/generations \
  -H 'Authorization: Bearer YOUR_PROJECT_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: pollinations-001' \
  -d '{
    "provider": "pollinations",
    "model": "sana",
    "prompt": "一只戴围巾的橘猫坐在窗边，电影感自然光",
    "n": 1,
    "size": "1024x1024",
    "response_format": "url"
  }'
```

在 **Gemini 账号 → 网页额度与平台连接** 中填写即梦 `sessionid`、通义万相完整 Cookie，或候选平台的网页 Token / Cookie；卡片会标出免费桶和验证状态，查询到的余额为 `0` 或未分类时不会提交。其他平台仍填写官方 Key。打开 **图片工作台** 可选择即梦 Seedream；打开 **视频工作台** 后，即梦支持首尾帧，Hailuo、MiniMax、Vidu 等按平台规则支持首尾帧或主体参考。任务记录会保存本地图片或 MP4。工作台调用本地管理接口，不需要额外填写项目 Key。

PixVerse credits 也可通过本机管理接口查询：`GET /api/video-providers/pixverse-web/credits`。返回 `credit_daily`、`credit_monthly`、`credit_package`、`free_available` 和三者合计；Token 不会写入返回值。生成只计算每日/月度免费余额，不会把套餐余额当成免费额度。

Hailuo 网页余额可通过 `GET /api/video-providers/hailuo-web/credits` 查询，返回登录赠送、订阅赠送、购买和可用免费余额；Token 不会写入返回值。若网页返回的额度没有分类，服务会拒绝提交，以免误扣购买 credits。

即梦网页余额可通过 `GET /api/video-providers/jimeng-web/credits` 查询，返回 `gift_credit`、`purchase_credit`、`vip_credit` 和合计，不会返回 `sessionid`。图片 API 使用 `provider: "jimeng-web"`，模型可传 `jimeng-seedream-5-flash`；视频 API 使用同一 provider，模型可传 `jimeng-seedance-2.5-draft`。

通义万相网页余额可通过 `GET /api/video-providers/tongyi-web/credits` 查询，返回 `bonus`、`member`、`top_up`、`free_available` 和合计，不会返回 Cookie。先在设置页保存从 `tongyi.aliyun.com` 复制的完整 Cookie Header（必须包含 `XSRF-TOKEN`），图片使用 `provider: "tongyi-web"`，视频也使用同一 provider；模型可传 `wan2.7-flash`、`wan2.6` 或 `wan3.0`。本地服务会在提交前确认奖励灵感值，免费桶不足时拒绝任务，不会改用充值灵感值。

通义万相图片示例：

```sh
curl --max-time 1200 http://127.0.0.1:8765/v1/images/generations \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: tongyi-image-001' \
  -d '{
    "provider": "tongyi-web",
    "model": "wan2.7-flash",
    "prompt": "一只戴围巾的橘猫坐在窗边，电影感自然光",
    "n": 1,
    "size": "2K",
    "response_format": "url"
  }'
```

Hailuo 网页图片示例（最多 1 张参考图；先在设置页保存 `_token`）：

```sh
curl --max-time 1200 http://127.0.0.1:8765/v1/images/generations \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: hailuo-image-001' \
  -d '{
    "provider": "hailuo-web",
    "model": "gpt-image-2",
    "prompt": "一只戴围巾的橘猫坐在窗边，电影感自然光",
    "n": 1,
    "size": "2K",
    "response_format": "url"
  }'
```

项目调用视频接口时，先用同一个项目 Key 上传参考图，再提交异步任务：

```sh
curl http://127.0.0.1:8765/v1/references \
  -H 'Authorization: Bearer YOUR_PROJECT_KEY' \
  -F 'files=@/absolute/path/first-frame.png'

curl http://127.0.0.1:8765/v1/video/jobs \
  -H 'Authorization: Bearer YOUR_PROJECT_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: video-test-001' \
  -d '{
    "provider":"minimax",
    "model":"default",
    "prompt":"人物自然走向镜头，镜头缓慢推进，保持脸部和服装稳定",
    "duration":5,
    "resolution":"720p",
    "aspect_ratio":"16:9",
    "audio":true,
    "reference_images":["ref_替换为上传返回的完整ID"]
  }'

curl http://127.0.0.1:8765/v1/jobs/vid_任务ID \
  -H 'Authorization: Bearer YOUR_PROJECT_KEY'
```

需要同步等待结果时使用 `POST /v1/videos/generations`；单个异步任务使用 `POST /v1/video/jobs`，多个视频建议使用 `POST /v1/video/batches` 后轮询 `GET /v1/video/batches/{id}`。视频结果在 `videos` 和 `data[].url` 中提供本地 MP4 地址。模型名称、时长和参考图数量会按平台校验，具体免费额度与账单由平台控制台决定。

视频批量调用会为每项保留 `custom_id`，整批最多 20 项，队列容量不足时整批拒绝，不会只提交一部分：

```sh
curl http://127.0.0.1:8765/v1/video/batches \
  -H 'Authorization: Bearer YOUR_PROJECT_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: video-batch-001' \
  -d '{"items":[
    {"custom_id":"opening","provider":"minimax","prompt":"人物走向镜头","duration":5,"audio":true},
    {"custom_id":"action","provider":"seedance","prompt":"人物完成一段连贯动作","duration":5,"reference_images":["ref_替换为上传返回的完整ID"]}
  ]}'

curl http://127.0.0.1:8765/v1/video/batches/vbatch_任务ID \
  -H 'Authorization: Bearer YOUR_PROJECT_KEY'
```

返回的 `object` 为 `video.batch`，`items[].data` 中是本地 MP4 URL；`status=completed` 只表示所有项目已结束，仍需检查各项 `status` 和 `counts.failed`。重复提交同一批参数时沿用相同的 `Idempotency-Key`，不会重复调用上游。

## 启动与配置

需要 Python 3.11+，推荐 3.12；已有 uv 时脚本会自动准备 Python 3.12 环境。

1. 在终端运行 `./start.command`，或在 macOS 双击此文件。在当前 Codex 工作区会复用 `work/venv`。
2. 打开 <http://127.0.0.1:8765> → **Gemini 账号** → **添加 Gemini 账号**。
3. 在浏览器登录 <https://gemini.google.com>，确认该账号能手动生图。
4. 打开开发者工具 → Application / 应用 → Storage → Cookies，选择 Gemini 域名。复制 `__Secure-1PSID` 和 `__Secure-1PSIDTS` 的 **Value** 到本地设置中。后者推荐填写；账号未提供时可留空。
5. 点击 **保存并测试连接**。网络需要代理时填写例如 `http://127.0.0.1:7890`。
6. 在同一页的 **网页额度与平台连接 → Google Labs ImageFX** 中，从已登录的 `labs.google/fx/tools/image-fx` 请求复制完整 `Cookie` Header，或填写该页面使用的短期 Bearer Token。服务会在每次生成前用 Cookie 换新的 session token；不需要浏览器持续打开。ImageFX 当前只支持文生图。
7. 在 **网页额度与平台连接 → Bing Image Creator** 中，从 `bing.com` Cookie 复制 `_U` 的 Value，只填写值并保存。Bing 使用网页 Boost；额度耗尽或账号受限时会直接返回网页额度错误，不会偷偷切到付费 API。
8. 在 **网页额度与平台连接 → Recraft 网页 Free plan** 中，从已登录网页请求的 `Authorization: Bearer ...` 复制 Token，只填写 Token 值并保存。每次生图前会查询 `/users/me`，只有 `plan.is_free=true` 且 `subscription_credits` 足够时才提交；购买/充值 credits 不会被使用。Free plan 图片可能公开展示，并按 Recraft 规则限制商用。
9. 在 **网页额度与平台连接 → Leonardo 网页 Free plan** 中，从已登录网页请求的 `Authorization: Bearer ...` 复制 JWT，只填写 Token 值并保存。每次生图前会查询 GraphQL 的 Free plan 与 `subscriptionTokens`；只有免费订阅桶足够时才提交。Leonardo 当前只支持文生图，网页生成的图片可能公开展示。
10. 在 **网页额度与平台连接 → 即梦 AI 网页** 中，从已登录的 `jimeng.jianying.com` Cookie 复制 `sessionid` 的 **Value**，只填写值并保存。页面可查询赠送、购买和会员余额；没有赠送积分时不会消耗付费积分。
11. 在 **网页额度与平台连接 → 通义万相网页** 中粘贴已登录网页的完整 Cookie Header（必须含 `XSRF-TOKEN`），在 **Vidu 网页（Q3 / Q2 免费额度）** 中粘贴 `www.vidu.cn` 已登录会话的完整 Cookie Header，或在 **Hailuo / MiniMax 网页** 中填写 `hailuoai.video` 的 localStorage `_token`。通义只使用 `bonus`；Vidu 只在确认免费桶大于 0 时提交；Hailuo 只使用登录/订阅赠送桶，购买额度不会被自动消耗。Vidu 网页服务直接请求 `service.vidu.cn`，不需要浏览器持续打开。
    豆包是实验性候选：在 **豆包网页** 的已登录请求中复制 Cookie、`device_id`、`web_id`（可连同 `fp`、`ms_token`、`tea_uuid`），在豆包卡片粘贴 JSON，例如 `{"cookie":"...","device_id":"...","web_id":"...","fp":"...","ms_token":"..."}`。这些值必须来自你自己的网页会话；本地服务不生成设备指纹、不实现 WebMSSDK/X-Bogus，也不绕过验证码。没有完整上下文或遇到验证挑战时任务会明确失败。
12. 如需 Gemini 双账号额度耗尽后的付费兜底，在 **Seedream 5.0 Flash** 区域填写火山方舟 API Key 并保存。Key 保存在本机私有数据目录，不会回显。
13. 在后台的 **项目与 Key** 区域填写项目名称，创建 Key 并复制保存。完整 Key 只显示一次。
14. 你的项目填写 Base URL `http://127.0.0.1:8765/v1` 和刚生成的 Key，再将模型、提示词、尺寸等参数放入请求体。也可以在工作台选择参数，下载完整配置，交给附带的 Python 客户端读取。

图生图：在图片工作台点击 **添加图片**，看到缩略图后填写修改要求并提交。[Gemini Apps 文档](https://support.google.com/gemini/answer/14903178?hl=zh-Hans)说明一次提示最多可上传 10 个文件（视功能可用性而定）；本服务接受最多 10 张静态 PNG / JPEG / WebP 参考图，每张最大 100 MB、最多 2500 万像素，本次上传合计最多 100 MB。后两个尺寸限制是本地网关限制；官方帮助页对非视频文件给出的大小上限是 100 MB。批量模式的每一行提示词共用所选参考图。上传时只保存到本机，提交任务时才发送到所选平台；生成结果仍取决于账号和模型能力。

这两个“Gemini 上限”对应不同产品：本服务登录的是 Gemini Apps 网页账号，按网页文档最多 10 个文件；官方开发者 [Gemini API 图像文档](https://ai.google.dev/gemini-api/docs/image-understanding)另写明模型每个请求最多 3600 张图像，但那是另一套 API 与计费方式，本网关不调用它。网页帮助没有给出统一的文字字符上限。本服务对 `prompt` 和 `negative_prompt` 分别限制 16000 字符；这是网关输入限制，不代表 Gemini 网页的公开最大长度。比例、尺寸和负面词都会变成提示词文本。

Cookie 是登录凭据，仅在你本地填写，不必发送到聊天中。服务只监听 `127.0.0.1`，没有开放远程访问。项目接口 `/v1/*` 允许本机 `http://localhost` / `http://127.0.0.1` 不同端口的跨域请求，仍需调用 Key；管理接口不向这些项目开放跨域访问。连接设置不会将 Cookie 回显到接口、页面或日志中。请在可信的本机环境运行。

也可以复制 `.env.example` 为 `.env` 填写环境变量。通过网页保存的配置优先于环境变量。`.env` 不参与源代码打包。

停止：在启动终端按 Ctrl+C。重新运行 `start.command` 即可恢复；已提交上游的任务不会在重启后自动重新生成；尚未提交的排队和临时限流等待任务恢复后继续。

### macOS 后台常驻

依赖准备好后，双击 `service.command`，或在项目目录运行 `./service.command install`。使用当前用户的 LaunchAgent（`local.gemini-web2api`）接管服务：现在立即启动，之后每次登录 macOS 自动启动，进程退出后自动恢复。关闭终端或 Codex 不影响后台服务；Gemini 账号池不要求浏览器持续运行。电脑关机或休眠时无法处理请求，开机登录或唤醒后恢复。

```sh
./service.command status     # 查看后台服务与 HTTP 健康状态
./service.command restart    # 空闲时重启
./service.command stop       # 暂时停止，下次登录仍自动启动
./service.command uninstall  # 取消常驻和登录启动，保留全部数据
```

明确需要立即升级时可运行 `./service.command restart --interrupt-running`：正在上游生成的任务会被中断并标记失败，网关不会自动重新生图；未发送的排队及临时限流等待任务恢复后继续。升级保留已安装服务的超时设置，显式环境变量可覆盖它。

管理文件位于 `~/Library/LaunchAgents/local.gemini-web2api.plist`；仅保存启动路径，不存储 Cookie 或调用 Key。为避开 macOS 对 Documents 后台访问的限制，常驻安装位于 `~/Library/Application Support/GeminiWeb2API/`，内含独立的 `app/`、`runtime/`、私有 `data/` 和 `images/`。首次安装复制已有账号、Key、任务与图片，之后以这里的数据为准，保留工作区原副本。私有日志位于 `data/logs/server.log`、`data/logs/error.log`。已启用后台常驻时，无需再运行 `start.command`。停止和重启会先检查生成队列，仍有任务时拒绝中断。源码更新后用 `./service.command restart` 将程序更新到常驻目录；不会覆盖已安装的业务数据。

## 多账号并行与智能切换

1. 原有账号自动迁移为“Gemini 账号 1”，无需重填。到 **Gemini 账号 → 添加 Gemini 账号** 填写第二个 Pro 账号的 Cookie，保存并测试。
2. 两个 worker 并行处理独立请求，每个账号同时只处理一个任务；空闲时优先账号 1，账号 1 忙碌时账号 2 仍可并行处理。批量提示词可以并发执行，整体最多两个任务。
3. 额度不足时，同一任务切换到其他可用账号，不需要项目更换 Key 或地址，也不会降低你请求的模型。部分完成的图片保留，只补剩余数量。
4. 生图和文字对话共用 Gemini 账号池。每个新请求优先尝试账号 1，再尝试账号 2；额度耗尽不会进入冷却，每次新请求都会重新从账号 1 开始。图像任务在本次请求的两个账号都明确返回额度耗尽时，如果已配置 Seedream API Key，就直接使用 Seedream 生成剩余图片。未配置 Seedream 时返回 `seedream_not_configured`。文字对话不能由图像模型处理，双账号额度耗尽后返回 `all_accounts_quota_exhausted`。Gemini 暂时限流、登录失败和网络错误均不会调用付费 Seedream。
5. Cookie 失效时跳过此账号，继续使用其他可用账号；账号页提示更新登录。短暂网络异常最多切换重试 3 次。上游超时无法确认是否已生成，只会切换到另一可用账号；没有备用账号时任务以 `result_uncertain` 结束，不会自动对同一账号重复生图。图片下载失败只重试同一图片的下载，不重新生图。
6. 尚未提交的排队任务及暂时受限后等待恢复的任务可以在重启后继续；额度耗尽会在本次请求内切换账号或触发 Seedream，不会等待冷却。正在上游生成的任务不会在重启后盲目重发。

账号的 Cookie 不回显、不写入客户端配置。两个不同登录会话的 Cookie 独立保存；重复填写同一份 Cookie 会被拒绝。添加账号时代理留空，沿用第一个启用账号的网络代理；编辑时 Cookie 留空保留现有信息。账号名、启用开关、冷却、最近错误和处理统计在账号页查看。

调用方式保持不变：`provider: "gemini"`（也可省略）、`model: "gemini-pro"` 或实际可用模型。Seedream 的 Base URL 和 model_id 固定为 `https://ark.cn-beijing.volces.com/api/v3` 与 `doubao-seedream-5-0-flash-260915`，不需在项目请求中传 Key 或单独选择 Seedream。推荐使用 `POST /v1/jobs` 或 `/v1/batches` 异步提交并轮询。重发相同业务请求必须沿用 `Idempotency-Key`。

当任务使用 Seedream 兜底时，历史记录会标记 `fallback_provider: "seedream"`，并在 `billing` 中记录成功生成张数和按官方单价估算的人民币金额。旧记录会尽量按已保存的 Seedream 图片补算；没有足够用量信息时显示无法估算。详情见 <https://ark.volcengine.com/region:cn-beijing/docs/ark/model-pricing#457edfd0>，最终账单以火山方舟控制台为准。

可配置网站适配器保留在账号页的高级选项内。内置 ChatGPT 接入已移除，不能再通过 `provider: "chatgpt"` 提交任务。

### 文字对话

```bash
curl --max-time 600 http://127.0.0.1:8765/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: chat-001' \
  -d '{"provider":"gemini","model":"default","messages":[{"role":"system","content":"用简洁中文回答。"},{"role":"user","content":"给我三个产品摄影创意"}],"stream":false}'
```

返回 `choices[0].message.content`、`job_id`、`provider`，没有可准确计算的网页 token 用量。支持 Gemini 以及启用了文字能力的自定义网站。多轮 messages 会序列化成文本上下文，每次请求发送到新的网页会话；不等同于官方 API 的原生 system role。暂不支持 streaming、tools、原生会话 ID 或官方 API 的其他推理参数。

长任务可用 `POST /v1/chat/jobs` 异步提交相同请求，随后 `GET /v1/jobs/{id}` 查询 `status` 和 `text`。`GET /v1/providers` 返回平台能力，`GET /v1/models?provider=gemini` 返回网页当前模型占位符。同步与异步调用都支持 Key 隔离和幂等。

## 一份配置用于单张与批量

网页的 **更多生成参数** 可以设置负面提示词、目标尺寸、提示词模式、原图下载和返回方式；模型、比例、数量与参考图也随每个任务传入。参数选择保存在当前浏览器，Cookie 和 Key 不写入浏览器本地存储。模型列表按账号实际发现结果显示。

点击 **预览实际提示词**，可先看完整请求和实际发送给 Gemini 的文本，不连接上游、不生成图片。批量模式在网页中预览第一条；客户端 `--preview` 可预览文件中的全部任务。

点击 **复制完整调用配置**，或在 API 区域 **下载配置 JSON**。将 `api_key` 填为后台创建的项目调用 Key，也可保留 `${GEMINI_WEB2API_KEY}` 并设置同名环境变量。示例见 `client-config.example.json`：

```json
{
  "base_url": "http://127.0.0.1:8765/v1",
  "api_key": "${GEMINI_WEB2API_KEY}",
  "parameters": {
    "model": "default",
    "n": 1,
    "prompt_mode": "enhanced",
    "negative_prompt": "文字、水印、模糊",
    "size": "2K",
    "aspect_ratio": "16:9",
    "full_size": true,
    "response_format": "url",
    "reference_images": []
  }
}
```

在项目目录运行（客户端只用 Python 标准库）：

```sh
export GEMINI_WEB2API_KEY='这里填写你的调用Key'
# 保存下载的配置为 client-config.json，先预览，不生成图片：
python3 api_example.py '生成一张咖啡产品海报' --config client-config.json --preview
# 去掉 --preview 即可提交生图；命令行显式参数覆盖配置：
python3 api_example.py '生成一张咖啡产品海报' --config client-config.json --size 4K --n 2
# 每条提示词共用配置中的参数，异步批量生成并保存结果：
python3 batch_example.py prompts.example.txt --config client-config.json --output batch-result.json
```

两个客户端都支持 `--model`、`--n`、`--negative-prompt`、`--size`、`--aspect-ratio`、`--prompt-mode`、`--full-size` / `--no-full-size`、`--response-format`、`--reference`、`--reference-id` 和 `--preview`。`--aspect-ratio auto` 清除比例要求。工作台导出的配置不含提示词，也不复制工作台私有参考图 ID；API 项目用自己的 Key 上传，可通过 `--reference` 传本地文件或将返回的 ID 放进配置。`--reference-id` 会替换配置中的参考图 ID，本地文件上传结果再追加到它们后面。

地址和 Key 只负责连接与鉴权。其他项目须将 `parameters` 的字段与 `prompt` 合并进请求 JSON；网页上的选择不会自动改变其他项目的请求。上面两个客户端已实现读取与合并。只支持地址和 Key 的第三方客户端，需要增加这些请求字段才能使用全部选项。

## HTTP 调用

API Base URL：`http://127.0.0.1:8765/v1`

### 生图

```sh
curl --max-time 1200 http://127.0.0.1:8765/v1/images/generations \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: my-image-001' \
  -d '{
    "model": "gemini-pro",
    "prompt": "生成一只白色陶瓷杯的产品摄影图，极简背景，柔和自然光",
    "n": 1,
    "prompt_mode": "enhanced",
    "negative_prompt": "文字、水印、模糊",
    "size": "2K",
    "aspect_ratio": "1:1",
    "full_size": true,
    "response_format": "url"
  }'
```

完成后返回：

```json
{
  "created": 1790000000,
  "data": [{"url": "http://127.0.0.1:8765/images/img_…/image_….png", "mime_type": "image/png", "width": 1024, "height": 1024}],
  "job_id": "img_…",
  "parameters": {"prompt": "…", "model": "…", "size": "2K", "full_size": true},
  "effective_prompt": "实际发送给 Gemini 的完整提示词…"
}
```

上面是响应格式示意。实际文件扩展名和尺寸以 Google 返回图片为准；不将 JPEG 字节伪装成 PNG。

使用即梦网页赠送积分时，只需把 provider 和 model 换成即梦值；不在请求中传网页 Cookie：

```sh
curl --max-time 1200 http://127.0.0.1:8765/v1/images/generations \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: jimeng-image-001' \
  -d '{
    "provider": "jimeng-web",
    "model": "jimeng-seedream-5-flash",
    "prompt": "生成一张白底陶瓷杯产品摄影图",
    "n": 1,
    "size": "2K",
    "response_format": "url"
  }'
```

Google Labs ImageFX 和 Bing Image Creator 也只需在本机配置一次网页凭据，项目请求不携带 Cookie：

```sh
curl --max-time 1200 http://127.0.0.1:8765/v1/images/generations \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: imagefx-image-001' \
  -d '{
    "provider": "imagefx-web",
    "model": "imagefx-imagen-3.5",
    "prompt": "白底陶瓷杯产品摄影，柔和自然光",
    "n": 1,
    "size": "1K",
    "response_format": "url"
  }'
```

Recraft 网页 Free plan 示例（先在设置页保存 Bearer Token；服务只使用 `subscription_credits`）：

```sh
curl --max-time 1200 http://127.0.0.1:8765/v1/images/generations \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: recraft-image-001' \
  -d '{
    "provider": "recraft-web",
    "model": "gpt_image_2p5_flare",
    "prompt": "白底陶瓷杯产品摄影，柔和自然光",
    "n": 1,
    "aspect_ratio": "1:1",
    "response_format": "url"
  }'
```

Leonardo 网页 Free plan 示例（先在设置页保存网页 Bearer Token；服务只使用 `subscriptionTokens`）：

```sh
curl --max-time 1200 http://127.0.0.1:8765/v1/images/generations \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: leonardo-image-001' \
  -d '{
    "provider": "leonardo-web",
    "model": "leonardo-diffusion-xl",
    "prompt": "白底陶瓷杯产品摄影，柔和自然光",
    "n": 1,
    "aspect_ratio": "1:1",
    "response_format": "url"
  }'
```

把 `provider` 换成 `bing`、把 `model` 换成 `bing-dalle3`、`bing-gpt4o` 或 `bing-mai-image-1`，即可使用 Bing 网页额度。ImageFX 和 Bing 当前不接受本地参考图；需要上传参考图请使用 `gemini` 或 `jimeng-web`。

| 参数 | 可选值与实际作用 |
|---|---|
| `provider` | 默认 `gemini`；可使用已配置的 `imagefx-web`、`bing`、`recraft-web`、`leonardo-web`、`jimeng-web`、`tongyi-web`、`hailuo-web`；Gemini 自动选择账号，Seedream 不作为请求 provider |
| `prompt` | 必填，1–16000 字符的非空提示词；`negative_prompt` 另有 16000 字符上限 |
| `model` | Gemini 使用 `gemini-pro` / `default`；即梦和通义图片使用各自网页模型。`GET /v1/models?provider=...` 需调用 Key |
| `n` | 1–4；串行生成，返回多张时复用已有结果，最多返回请求数量 |
| `negative_prompt` | 可选；增强模式将它作为“请避免以下内容”追加到提示词，未使用 Google 独立负面参数 |
| `size` | `auto`（默认）、`1K` / `2K` / `4K` 或 `宽x高`（每边 64–8192）。只作为生成目标写入提示词，不保证实际像素，不做缩放 |
| `aspect_ratio` | `null`（默认）、`1:1`、`16:9`、`9:16`、`4:3`、`3:4`、`3:2`、`2:3`。作为提示词要求；若与尺寸目标冲突，以此比例要求为准 |
| `prompt_mode` | `enhanced`（默认）补充生图、参考图编辑与构图指令；`raw` 原样传递 `prompt`，保留空格和换行 |
| `full_size` | Gemini：`true`（默认）请求原图，`false` 下载预览；可能回退为较小图片。浏览器平台以网页可读文件为准 |
| `response_format` | `url`（默认）或 `b64_json`；两种形式都附实际 `width`、`height`、`mime_type`，图片也保存在本地 |
| `reference_images` | 使用当前项目 Key 上传后得到的参考图 ID 数组，最多 10 个；每张最大 100 MB / 2500 万像素，本次上传总计最大 100 MB |

`raw` 模式必须同时设置 `size="auto"`、`aspect_ratio=null`、`negative_prompt=""`（或省略这三项），否则返回明确的 422，不会静默忽略参数。模型选择、参考图、数量和下载质量仍然生效。尺寸只使用提示词表达；历史 `1792x1024` / `1024x1792` 保留原有 16:9 / 9:16 的构图解释，其他自定义尺寸会在未指定比例时推算宽高比。

模型、提示词和参考图均相同，也不能保证与网页每次生成的画面完全一致。本网关不控制随机种子，网页还可能使用不同的隐藏指令或后处理。

- 同步请求会等到任务完成；排队时间也计算在调用端超时内。批量调用更适合使用异步任务接口。
- 推荐每个业务操作设置唯一 `Idempotency-Key`。连接丢失后重复请求需沿用该键和相同参数；服务会返回原任务，不重复生图。相同键配不同参数返回 409；如需新图，请使用新键。

Python 直接调用（环境变量中填写后台生成的调用 Key）：

```sh
export GEMINI_WEB2API_KEY='这里填写你的调用Key'
python3 api_example.py '生成一幅水彩风格的湖边小屋插画'
```

使用 OpenAI Python SDK 的图片调用格式（可选安装 SDK）：

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8765/v1",
    api_key=os.environ["GEMINI_WEB2API_KEY"],  # 后台生成的项目调用 Key
    timeout=1200,
    max_retries=0,
)
result = client.images.generate(
    model="gemini-pro",
    prompt="生成一幅极简风格的咖啡海报",
    n=1,
    response_format="url",
    extra_headers={"Idempotency-Key": "coffee-poster-001"},
)
print(result.data[0].url)
```

提供图片生成、参考图编辑、批量任务、模型列表、非流式文字对话和项目 Key 管理；未支持的参数会明确返回 422。参考图上传与生成采用下方两步接口，不提供 `/v1/images/edits`。聊天接口见前文，网页会话不作为官方原生多轮会话维护。

### 不生图的请求预览

向 `POST /v1/requests/preview` 携带项目 Key，提交与生图相同的请求 JSON，返回 `parameters`、`effective_prompt` 和 `warnings`。验证参数及参考图归属，不连接 Google，也不创建任务或消耗生图额度。所有新增参数同样适用于 `/v1/jobs` 和批量接口中每个 item；批次中的各项可指定不同参数。

### 上传参考图＋提示词

第一步上传本地图片，不消耗生成额度：

```sh
curl http://127.0.0.1:8765/v1/references \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -F 'files=@/absolute/path/reference.png'
# 多张图片可重复添加 -F 'files=@另一张图片的路径'
```

返回 `{"data":[{"id":"ref_…","name":"reference.png","mime_type":"image/png","width":1200,"height":800,"bytes":12345,"created_at":1790000000}]}`。这是响应格式示意。

第二步把返回的 ID 放入 `reference_images`，与提示词一起提交：

```sh
curl --max-time 1200 http://127.0.0.1:8765/v1/images/generations \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: product-edit-001' \
  -d '{
    "prompt":"保留参考图中的陶瓷杯，将背景改成浅绿色，柔和自然光",
    "model":"gemini-pro",
    "reference_images":["ref_这里替换为上传返回的完整ID"],
    "n":1
  }'
```

`POST /v1/jobs` 和 `/v1/batches` 中的每个 item 同样接受 `reference_images`。同一 ID 可在同一项目 Key 的多个任务中复用；不同 Key 之间的参考图相互隔离。ID 可用 `GET /v1/references/{id}/content` 携带同一 Key 读取，重启后仍可复用。工作台上传的参考图供工作台自身的任务使用；API 项目应使用自己的 Key 上传。

OpenAI SDK 的 `images.generate` 可通过 `extra_body={"reference_images":["ref_完整ID"]}` 传入这些 ID。参考图 ID 不是图片 URL 或服务器文件路径。

附带的 Python 客户端支持直接上传并生成：

```sh
export GEMINI_WEB2API_KEY='这里填写你的调用Key'
python3 api_example.py '保留主体，把背景改成浅绿色' --reference '/absolute/path/reference.png'
# 多张参考图可以重复传入 --reference。
python3 batch_example.py prompts.example.txt --reference '/absolute/path/reference.png'
```

客户端会打印上传后的参考图 ID。若要在连接中断后重复请求，应同时沿用原 `--idempotency-key`、原 `--reference-id` 和相同参数；不要重新上传后换一个 ID。批量任务优先用 `--batch-id` 继续查询原批次。

### 异步任务（推荐批量调用）

```sh
curl http://127.0.0.1:8765/v1/jobs \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: batch-task-001' \
  -d '{"prompt":"生成一只猫的水彩插画","model":"gemini-pro","n":1}'

# 将返回的 id 替换到下方。
curl http://127.0.0.1:8765/v1/jobs/img_任务ID \
  -H 'Authorization: Bearer YOUR_API_KEY'
```

查询 `/v1/jobs/{id}` 会在 `data` 中返回图片 URL / Base64。状态依次为 `queued` → `running` → `succeeded` / `failed`。`images` 是相对于 `/images/` 的路径。可以用 `POST /v1/jobs/{id}/cancel` 取消尚未执行的任务。已经发送到 Google 的生成请求不支持撤销；关闭页面或调用端断开不会取消任务。

若 Gemini 对生图请求只返回了文字，任务会以 `failed` 结束，但 `text` 与 `error.response` 保留原回复。轮询时应以 `status` 判断任务是否终结，并在 `failed` 时读取这两个字段。健康接口 `/health` 会返回当前 `generation_timeout_seconds` 和 `limits`，包括提示词、参考图（最多 10 张）、单次输出图片（最多 4 张）、批量、排队和文字对话长度上限。文字对话每条消息最多 32,000 字符、总计最多 64,000 字符、最多 80 条；本地工作台会显示字数并按服务限制输入。任务记录页显示运行计时；失败或取消的任务可复制 Gemini 回复，或用原参数和原参考图重新提交。重新提交会再次使用网页账号额度。

失败或取消的任务可通过 `POST /v1/jobs/{id}/retry` 重新提交（需同一项目 Key），或在本地任务记录页点击「重新提交」。建议为每次重试使用新的 `Idempotency-Key`；重复发送同一个重试键会返回同一新任务。运行中和已完成的任务不可重试。

接口：

| 接口 | 用途 |
|---|---|
| `GET /health` | 服务、配置、连接、队列状态；不发起生图 |
| `POST /api/config` | 在本机保存 `secure_1psid`、`secure_1psidts`、`proxy` |
| `POST /api/connect` | 验证登录并发现模型；不发起生图 |
| `GET /api/models` | 后台读取账号模型，供页面刷新后恢复列表 |
| `GET /v1/models` | 发现账号可用模型（需调用 Key） |
| `POST /v1/requests/preview` | 验证完整参数并预览实际提示词，不生图（需调用 Key） |
| `POST /api/requests/preview` | 后台自身任务的提示词预览 |
| `POST /v1/references` | multipart `files` 上传参考图，返回 ID（需调用 Key） |
| `GET /v1/references/{id}/content` | 读取本项目上传的参考图（需调用 Key） |
| `POST /v1/batches` | 一次提交最多 20 条提示词（需调用 Key） |
| `GET /v1/batches/{id}` | 查询批次与各项图片结果（需调用 Key） |
| `POST /api/keys` | 本地后台：创建项目调用 Key |
| `GET /api/keys` | 本地后台：Key 列表，不回显完整 Key |
| `POST /api/keys/{id}/revoke` | 本地后台：停用 Key |
| `POST /v1/images/generations` | 等待生图并返回 URL / Base64 |
| `POST /v1/jobs` | 提交异步生图任务（需调用 Key） |
| `GET /api/video-providers` | 本地后台：视频平台、文档和配置状态，不回显 Key |
| `GET /api/web-image-providers` | 本地后台：ImageFX / Bing / Recraft / Leonardo 网页图片平台状态，不回显凭据 |
| `PUT /api/web-image-providers/{provider}` | 本地后台：保存 ImageFX Cookie/Token、Bing `_U` Cookie、Recraft 或 Leonardo Bearer Token |
| `DELETE /api/web-image-providers/{provider}` | 本地后台：清除 ImageFX / Bing / Recraft / Leonardo 网页凭据 |
| `PUT /api/video-providers/{provider}` | 本地后台：保存网页 session / Token / Cookie Header 或官方视频 API Key；即梦使用 `auth_type: web_session`，Kling 使用 `auth_type: web_cookie` |
| `GET /api/video-providers/{provider}/credits` | 本地后台：查询 PixVerse、即梦或通义万相网页余额，不回显凭据 |
| `POST /api/video/requests/preview` | 本地后台：校验视频参数，不提交上游任务 |
| `POST /api/video/jobs` | 本地后台：提交异步图生视频任务 |
| `POST /api/video/batches` | 本地后台：提交视频批次 |
| `GET /api/video/batches/{id}` | 本地后台：查询视频批次 |
| `POST /v1/videos/generations` | 等待视频生成并返回本地 MP4 URL（需调用 Key） |
| `POST /v1/video/jobs` | 提交异步视频任务（需调用 Key） |
| `POST /v1/video/batches` | 一次提交最多 20 个视频任务（需调用 Key） |
| `GET /v1/video/batches/{id}` | 查询视频批次与各项 MP4 结果（需调用 Key） |
| `GET /api/jobs` | 最近 100 个任务 |
| `GET /v1/jobs/{id}` | 查询单个任务 |
| `POST /v1/jobs/{id}/cancel` | 取消排队任务 |
| `POST /v1/jobs/{id}/retry` | 使用原请求参数重新提交失败或取消的任务（需调用 Key） |
| `GET /images/{path}` | 获取本地已生成图片 |
| `GET /videos/{path}` | 获取本地已保存视频 |
| `GET /docs` | 交互式接口文档 |

## 一次提交多条提示词

项目调用批量接口后立即获得 `batch_id`，可按 `custom_id` 将图片对应回项目中的商品、页面或文章。

```sh
curl http://127.0.0.1:8765/v1/batches \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: my-batch-001' \
  -d '{"items":[
    {"custom_id":"product-cover","prompt":"生成一只白色陶瓷杯的产品图","n":1},
    {"custom_id":"home-banner","prompt":"生成一张护肤品海报","aspect_ratio":"16:9"}
  ]}'

curl http://127.0.0.1:8765/v1/batches/batch_这里替换为返回的ID \
  -H 'Authorization: Bearer YOUR_API_KEY'
```

查询返回 `status`、`counts` 和 `items`。每项都包含 `custom_id`、`job_id`、`status`、`data`（图片 URL / Base64）、`error`、`parameters` 与 `effective_prompt`。`status=in_progress` 表示继续查询；`completed` 表示所有项目均已结束，**需同时检查各项的 `status` 与 `counts.failed`**。一项失败不会阻止后续项目。

每批 1–20 条，每条最多 10 张参考图。`custom_id` 在批内须唯一。队列装不下整批时返回 429，不接受其中任何一条。重发相同批次须沿用 `Idempotency-Key` 和全部相同参数。

也可直接运行附带的批量客户端：

```sh
export GEMINI_WEB2API_KEY='这里填写你的调用Key'
python3 batch_example.py prompts.example.txt --output batch-result.json
# 查询未完成的原批次，不重复生图：
python3 batch_example.py --batch-id batch_你的ID
```

后台可直接切换 **批量提示词**，每行填写一个任务，最多 20 条后统一提交。

## 任务与数据

- 两个 worker 并行执行；每个账号同时一个请求，最多 20 个任务等待。每张图默认等待 450 秒，最高 600 秒。Gemini 在生成期间持续返回文字时，记录会显示已收到的回复；识别到已完成的拒绝或读到已保存的最终回复后，会尽快结束任务。可用 `WEB2API_TIMEOUT` 调整（30–600 秒）。
- 重试由账号池控制；额度不足、登录失效和短暂网络错误按上面的规则切换。结果不确定的上游超时不会自动重试同一账号，需先检查 Gemini 历史。已关闭依赖库不受控的隐式重试。
- 一个任务部分完成后失败，会保留已下载图片，错误响应中含 `job_id` 与已完成图片，避免丢失结果。
- Key 在本地只保存 SHA-256 摘要，完整值仅创建时返回。不同 Key 的任务查询和幂等键相互隔离；后台可查看全部任务。
- 管理后台无需单独密码，仅供可信本机使用；不要将端口通过隧道或端口转发公开。
- 任务历史和配置默认位于 `.local/`；在当前 Codex 工作区通过启动脚本运行时位于工作区的 `work/gemini-web2api-data/`。
- Cookie 缓存位于私有数据目录的 `cookies/`，由依赖库维护。配置和任务记录文件权限为 `0600`，私有数据目录为 `0700`。
- 图片默认在项目 `generated/`。`WEB2API_STATE_DIR`、`WEB2API_IMAGES_DIR` 可指定绝对路径。
- 参考图原件保存在私有数据目录的 `references/`，索引在 `references.json`，不在公开图片目录中。界面的移除按钮取消本次选用；已上传原件保留在本机，便于排队任务与 API 复用。
- 网页账号生成的内容也可能出现在 Google 的对话历史中，遵循该账号的网页设置。

错误使用统一 JSON：`{"error":{"code":"…","message":"…"}}`。缺少或无效调用 Key 返回 401，未配置账号返回 503，登录失效通常为 401，队列满为 429。文字任务在本次尝试完全部账号且额度耗尽后返回 `all_accounts_quota_exhausted`；图像任务会调用已配置的 Seedream 兜底。不会返回原始上游异常或 Cookie。

## 验证与许可

测试使用假的上游后端，验证 HTTP 请求、项目 Key 鉴权与隔离、参考图上传与实际传参、格式和大小校验、批量任务、串行执行、URL / Base64、图片下载、幂等、错误与重启恢复，以及平台路由、浏览器配对、即梦 session 隔离、赠送积分保护、原生图片路由、单次领取租约、结果重放、网页超时、参考图权限、文字对话与部分结果保留。运行 `python -m pytest tests/`（需要 `pytest`、`httpx`、`pytest-asyncio`）和 `node --test tests/test_extension.cjs`。扩展 worker 测试覆盖结果回传重试不重发提示词、重启中断和消息来源校验。自动化测试不登录模型账号，也不消耗生图额度。

多账号测试覆盖账号并行且各自独占、每次请求的账号顺序和额度切换、双账号额度耗尽后的 Seedream 兜底、并发上限、参考图和部分结果保留、登录与网络切换、账号持久化、重复登录检查和凭据脱敏。

本项目源代码以 AGPL-3.0-or-later 提供，见 `LICENSE`。上游依赖项目：[Gemini-API](https://github.com/HanaokaYuzu/Gemini-API)。所有服务端与前端源代码均包含在项目目录及旁边的源代码 ZIP 中；工作台侧栏提供源代码入口。
