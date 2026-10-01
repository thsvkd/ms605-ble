# MS605 GUI API (M2)

`docs/GUI_PLAN.md`의 M2(GUI 뼈대)를 구현하기 위한 계약 문서다. 백엔드 구현자(`ms605/gui/*.py`)와
프런트엔드 구현자(`web/**`)는 서로 묻지 않고 이 문서만 보고 동시에 작업한다. 서버는 `docs/CORE_API.md`의
코어 표면(`Fleet`, `Registry`, `Storage`, `EventBus`, `SimFleet`)을 감싸기만 하고, 코어 모듈은 고치지 않는다.
설계 결정 D1~D15를 전제로 하며, 여기서 새로 정한 것은 G 번호와 이유를 함께 적는다.

이 저장소는 공개 저장소다. 이 문서, 테스트, 프런트엔드 fixture의 예시 값(Device ID, 주소, 사이트 이름, 토큰, IP)은
모두 합성 값이다. 시뮬레이터의 Device ID(`53494d3630350001` = `b"SIM605" + 1`)와 주소(`02:00:00:00:00:01`)를 쓴다.

## 1. 새로 정한 것

| # | 주제 | 결정 | 이유 |
|---|------|------|------|
| G1 | 토큰 범위 | `--lan`이 없어도 **항상 토큰을 요구**한다. CLI가 출력하는 localhost URL에 토큰이 들어 있고, 브라우저는 쿠키로 기억한다 | 같은 컴퓨터의 다른 사용자·프로세스, 그리고 브라우저의 다른 탭이 `127.0.0.1`에 보내는 요청(CSRF)을 막는다. 인증 경로가 하나뿐이라 테스트도 한 가지만 하면 된다 |
| G2 | 토큰 전달 | 첫 접속만 `?t=`, 서버가 `HttpOnly` 쿠키로 바꿔 주고 주소에서 지운다. 스크립트·테스트는 `Authorization: Bearer` | JS가 토큰을 다루지 않는다. WS 핸드셰이크에도 쿠키가 실리므로 WS 전용 인증 경로가 필요 없다 |
| G3 | 상태 전파 | 서버가 코어 이벤트를 받아 **완성된 뷰(`SensorView` 등)를 upsert 메시지**로 보낸다. 코어 이벤트를 날것으로 보내지 않는다 | 클라이언트 리듀서가 "받은 것으로 바꿔 끼우기"만 하면 된다. 뷰는 항상 그 순간의 코어 상태로 다시 만들므로, 이벤트 순서나 유실에 덜 민감하다(D11) |
| G4 | 순서 번호 | `seq`는 **서버 전역** 단조 증가 번호다. 스냅샷도 `seq`를 갖고, 그 뒤 메시지는 `seq+1`부터다 | 끊김·유실을 클라이언트가 숫자 하나로 판단한다. 재접속은 항상 스냅샷부터 받으므로 재동기화 경로가 하나다 |
| G5 | 느린 클라이언트 | 메시지를 골라 버리지 않고 **그 클라이언트의 연결을 끊는다**(1013). 클라이언트는 곧바로 다시 붙어 스냅샷을 받는다 | 상태 메시지를 하나라도 버리면 화면이 조용히 틀린다. 끊고 다시 받는 것이 가장 단순하고 항상 맞다 |
| G6 | 타입 생성 | pydantic 모델 → `models_json_schema()`로 만든 OpenAPI 문서(`web/openapi.json`) → `openapi-typescript`. FastAPI 라우트에서 뽑지 않는다 | WS 메시지 타입은 FastAPI의 OpenAPI에 나오지 않는다. 라우트 없이 `schemas.py`만으로 타입이 나오므로 프런트엔드가 백엔드 완성을 기다리지 않는다 |
| G7 | 업로드 | yaml 가져오기는 multipart가 아니라 **JSON 본문(`content` 문자열)** 이다 | `python-multipart` 의존성이 필요 없고, 요청 타입도 다른 API와 같은 방식으로 생성된다. 파일은 수 KB다 |
| G8 | 시뮬레이터 데이터 | `--sim`이면 `data_root()/cal_results/sim/`을 저장소 루트로 쓴다 | 데모가 실제 `registry.json`에 가짜 센서를 섞지 않는다. 고정 경로라 e2e 테스트가 찾을 수 있다 |
| G9 | 포트 | CLI가 소켓을 직접 bind·listen한 뒤 uvicorn에 넘긴다. `--port 0`을 허용한다 | 포트 충돌을 깔끔한 오류(종료 코드 2)로 낼 수 있고, e2e 테스트가 빈 포트를 안전하게 얻는다 |
| G10 | 프런트 상태 | Zustand 스토어 하나 + 순수 함수 리듀서. 스토어는 **WS 메시지로만** 바뀐다. REST 응답은 폼의 성공·실패 표시에만 쓴다 | 서버가 기준이다(D11). 낙관적 갱신이 없으니 두 화면이 어긋날 길이 없다 |

## 2. 파일 구성과 소유권

```
ms605/gui/
  __init__.py      모듈 docstring만. 다시 내보내기 없음
  schemas.py       pydantic 모델(5장 그대로) + openapi_document() + `python -m ms605.gui.schemas <out>`
  server.py        create_app(), 인증·Host·Origin 검사, REST 라우트, 오류 처리, 정적 파일
  ws.py            Hub(이벤트 → 메시지, 클라이언트 큐, seq), /ws 핸들러
  cli.py           add_parser(sub), run_gui(args, ...) — `ms605 gui`
  static/          빌드된 SPA (커밋한다. 직접 고치지 않는다)
web/
  package.json, package-lock.json, tsconfig.json, vite.config.ts, index.html
  openapi.json     schemas.py에서 생성 (커밋)
  src/
    main.tsx               진입점: 테마 적용, WS 연결 시작, <App/>
    App.tsx                AppShell + 라우트
    api/schema.ts          생성 파일 (커밋, 직접 고치지 않는다)
    api/types.ts           생성 타입의 짧은 별칭 (export type SensorView = components["schemas"]["SensorView"] …)
    api/client.ts          REST 호출 함수와 ApiRequestError
    api/ws.ts              WS 연결·재연결·seq 검사
    store/reducer.ts       순수 함수 reduce(state, message)
    store/store.ts         Zustand 스토어와 셀렉터
    strings.ts             모든 한국어 문구 (D15)
    format.ts              상대 시간, 펌웨어·배터리 표기
    status.ts              SensorView → 상태(종류·아이콘·문구) 판정
    theme.ts               라이트/다크/시스템 전환
    screens/Dashboard.tsx, screens/Gather.tsx, screens/SensorDetail.tsx
    components/*.tsx       9.4절 목록
    styles/tokens.css, styles/global.css   (컴포넌트 스타일은 *.module.css)
    test/setup.ts, test/fixtures.ts
tests/
  test_gui_auth.py, test_gui_api.py, test_gui_ws.py, test_gui_schema.py, test_gui_e2e.py, test_gui_static.py
```

| 소유 | 파일 | 비고 |
|------|------|------|
| 백엔드 | `ms605/gui/*.py`, `ms605/cli/cli.py`(8.1절의 두 곳만), `pyproject.toml`, `uv.lock`, `web/openapi.json`, `tests/test_gui_{auth,api,ws,schema,e2e}.py` | 가장 먼저 `schemas.py`를 5장 그대로 쓰고 `web/openapi.json`을 만든다 |
| 프런트엔드 | `web/**`(`openapi.json` 제외), `ms605/gui/static/**`, `.gitignore`에 `web/node_modules/` 한 줄, `tests/test_gui_static.py` | `schemas.py`가 생기기 전에는 5장을 보고 fixture와 화면을 만들고, 생기면 `npm run typegen` |

계약을 바꿔야 하면 이 문서를 먼저 고치고 양쪽이 따른다. 5장의 모델은 필드 하나라도 이 문서와 달라지면 안 된다.

## 3. 의존성과 패키징

### 3.1 Python

```
uv add fastapi uvicorn websockets segno "pydantic>=2.7"
uv add --dev httpx pytest-timeout
```

- `uvicorn`은 `[standard]` 없이 쓴다. uvloop를 쓰면 안 되기 때문이다(8.3절). WS 구현은 `websockets`다.
- `segno`: 의존성 없는 QR 생성기. 터미널 출력(`terminal(compact=True)`)을 기본 제공한다.
- `httpx`: FastAPI `TestClient`와 e2e 테스트의 HTTP 클라이언트. `pytest-timeout`: WS 테스트의 `receive`는 시간 제한이 없으므로 멈춤을 막는 안전망이다.
- `pyproject.toml`:

```toml
[tool.setuptools]
packages = ["ms605", "ms605.cli", "ms605.gui"]

[tool.setuptools.package-data]
"ms605.gui" = ["static/*", "static/**/*"]
```

- FastAPI의 `Depends`를 기본 인자로 쓰면 ruff B008에 걸린다. 핸들러는 `request: Request`를 받아 `request.app.state`에서
  꺼내거나 `Annotated[..., Depends(...)]`를 쓴다. ruff 설정은 바꾸지 않는다.

### 3.2 web

Node 22, npm. 의존성은 설치 시점의 안정판으로 받고 `package-lock.json`을 커밋한다.

- dependencies: `react`, `react-dom`, `zustand`, `wouter`, `lucide-react`
- devDependencies: `vite`, `@vitejs/plugin-react`, `typescript`, `@types/react`, `@types/react-dom`, `vitest`, `jsdom`,
  `@testing-library/react`, `@testing-library/user-event`, `@testing-library/jest-dom`, `openapi-typescript`

`package.json` scripts (이 이름과 동작 그대로):

```json
{
  "dev": "vite",
  "build": "tsc -b && vite build",
  "typecheck": "tsc -b",
  "test": "vitest run",
  "typegen:openapi": "uv run --project .. python -m ms605.gui.schemas openapi.json",
  "typegen": "npm run typegen:openapi && openapi-typescript openapi.json -o src/api/schema.ts"
}
```

`tsconfig`: `strict: true`, `noUncheckedIndexedAccess: true`, `noEmit: true`.

`vite.config.ts`의 요점:

```ts
const dropOrigin = (proxy) => {
  proxy.on('proxyReq', (req) => req.removeHeader('origin'))
  proxy.on('proxyReqWs', (req) => req.removeHeader('origin'))
}
export default defineConfig({
  plugins: [react()],
  build: { outDir: '../ms605/gui/static', emptyOutDir: true, assetsDir: 'assets' },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8605', changeOrigin: true, configure: dropOrigin },
      '/ws': { target: 'ws://127.0.0.1:8605', ws: true, changeOrigin: true, configure: dropOrigin },
    },
  },
  test: { environment: 'jsdom', setupFiles: ['src/test/setup.ts'] },
})
```

개발 흐름: `uv run ms605 gui --sim 3`을 띄우고, 출력된 `http://127.0.0.1:8605/?t=…`를 한 번 열어 쿠키를 받은 뒤
`npm run dev`의 `http://127.0.0.1:5173/`을 연다. 쿠키는 포트를 가리지 않으므로 5173에도 실리고, `changeOrigin`이
Host를 `127.0.0.1:8605`로 바꾸므로 쿠키 이름(4.2절)과 Host 검사가 맞는다. 프록시는 `Origin`을 지운다(없는 Origin은 허용, 4.3절).

### 3.3 정적 파일 배포

- `npm run build`가 `ms605/gui/static/`을 통째로 다시 만든다(`index.html`, `assets/*`, `favicon.svg`). 이 결과물을 **커밋**한다.
  그래서 Node 없이 `uv run ms605 gui`가 동작한다. `web/` 소스를 바꾸면 같은 변경에서 빌드 결과도 커밋한다.
- 서버는 `importlib.resources.files("ms605.gui") / "static"`을 쓴다(`create_app(static_dir=...)`로 바꿀 수 있다, 테스트용).

## 4. 인증과 접속 보안

### 4.1 토큰

- `run_gui()`가 실행할 때마다 `secrets.token_urlsafe(32)`로 만든다. 메모리에만 있고 파일·로그에 남기지 않는다(터미널 출력 제외).
- 수명은 프로세스 수명이다. 서버를 다시 띄우면 이전 토큰과 쿠키는 모두 무효가 된다.
- 비교는 `secrets.compare_digest`.

### 4.2 전달

| 경로 | 받는 방법 |
|------|-----------|
| 브라우저 첫 접속 | `GET <아무 비API 경로>?t=<token>`. 서버가 검사한 뒤 쿠키를 심고 `t`를 뺀 같은 경로로 `303` 리다이렉트한다(다른 쿼리는 유지. 경로 앞의 `/`는 하나로 줄인다. `//host/x`는 다른 사이트로 가는 주소이기 때문이다). `t`가 틀리면 쿠키 없이 같은 리다이렉트만 한다. SPA가 401을 받아 안내 화면을 띄운다 |
| 브라우저 이후 요청 | 쿠키 `ms605_token_<port>`. `HttpOnly; SameSite=Lax; Path=/`, 만료 없음(브라우저 세션 쿠키). `<port>`는 요청 Host 헤더의 포트(없으면 80) |
| 스크립트·테스트 | `Authorization: Bearer <token>` (REST와 WS 핸드셰이크 모두) |

- 쿠키 이름에 포트를 넣는 이유: 쿠키는 포트를 가리지 않으므로, 같은 컴퓨터에서 두 서버를 띄우면 서로의 쿠키를 덮어쓴다.
- `SameSite=Strict`가 아니라 `Lax`인 이유: 폰 카메라 앱에서 QR을 열면 교차 사이트 이동이다. `Strict`는 그 뒤의 리다이렉트
  요청에 쿠키를 싣지 않아 첫 화면이 401이 된다. 교차 사이트 POST는 `Lax`와 Origin 검사가 막는다.
- WS 전용 토큰 전달(쿼리, 첫 메시지)은 두지 않는다. 같은 출처의 WS 핸드셰이크에는 쿠키가 실린다.

### 4.3 검사 순서와 실패 동작

모든 요청에 아래를 차례로 적용한다.

1. **Host**: Starlette `TrustedHostMiddleware(allowed_hosts=...)`. 기본은 `["127.0.0.1", "localhost"]`, `--lan`이면
   여기에 LAN IP(`--lan-host`를 주면 그것과 자동으로 찾은 IP 둘 다), 호스트 이름, 호스트 이름 + `.local`을 더한다. 호스트 이름은 `socket.gethostname()`을 소문자로 바꾸고 끝의 `.local`을
   뗀 것이다(브라우저는 Host를 소문자로 보내고, 미들웨어는 대소문자를 가린다). 어긋나면 `400` 평문
   `Invalid host header`(미들웨어 기본 동작, JSON 아님). DNS rebinding을 막는다. `/ws` 핸드셰이크는 거절 응답 대신
   accept 전에 `close(1008)`로 막는다(uvicorn은 HTTP 403으로 답한다. 거절 응답은 uvicorn이 매번 ERROR로 기록해 QR이 있는 터미널을 덮는다).
2. **Origin**: `/api/*`의 `GET/HEAD/OPTIONS`가 아닌 요청과 `/ws`에서, `Origin` 헤더가 **있으면**
   `f"http://{request.headers['host']}"`와 정확히 같아야 한다(`Origin: null` 포함 다르면 거절). REST는
   `403 forbidden_origin`, WS는 accept 후 즉시 `close(4403)`. `Origin`이 없는 요청(테스트, curl, Vite 프록시)은 통과한다.
3. **토큰**: `/api/health`, 정적 파일, SPA 셸(`/`와 비API 경로)은 토큰 없이 연다. 그 밖의 `/api/*`는 쿠키 또는 Bearer가
   맞아야 하고, 아니면 `401` + `ApiError{code:"unauthorized"}`. 토큰이 맞아도 `Content-Length`가 512 KiB(`MAX_BODY_BYTES`)를 넘으면
   본문을 읽기 전에 `413` + `ApiError{code:"invalid_request"}`(가장 큰 본문인 65536자 가져오기도 그보다 훨씬 작다. `Content-Length` 없는
   chunked 본문은 검사하지 않는다). `/ws`는 accept 후 즉시 `close(4401)`.
   (accept 전에 닫으면 브라우저는 1006만 보므로 이유를 알 수 없다. accept 뒤에는 어떤 데이터도 보내지 않는다.)

CORS 미들웨어는 두지 않는다(같은 출처만 쓴다). FastAPI의 `/docs`, `/redoc`, `/openapi.json`은 끈다
(`docs_url=None, redoc_url=None, openapi_url=None`). 타입의 기준은 `openapi_document()`다.

알려진 한계: `--lan`은 HTTP이므로 같은 네트워크에서 토큰을 엿볼 수 있다. v1은 신뢰하는 LAN에서만 쓴다고 보고, HTTPS는 범위 밖이다.

### 4.4 터미널 출력과 QR

`run_gui()`는 서버가 listen을 시작한 뒤(8.2절 5단계) 아래를 `print(..., flush=True)`로 출력한다. **첫 줄의 형식은 고정**이다(e2e 테스트가 파싱한다).

```
ms605 gui: http://127.0.0.1:8605/?t=<token>
시뮬레이터: 센서 3대, 속도 x20                      (--sim일 때만)
LAN 주소: http://192.0.2.10:8605/?t=<token>         (--lan일 때만)
<QR: LAN 주소 URL 전체>                              (--lan일 때만)
폰에서 열리지 않으면(VPN 등) --lan-host <이 컴퓨터의 Wi-Fi 주소>로 다시 실행하세요.   (--lan이고 --lan-host가 없을 때만)
주의: 이 주소를 가진 사람은 누구나 센서를 조작할 수 있습니다. 공유하지 마세요.   (--lan일 때만)
종료하려면 Ctrl-C를 누르세요.
```

- QR 내용은 LAN URL 문자열 그대로(`http://<lan-ip>:<port>/?t=<token>`)다. `segno.make(url, error="m").terminal(out=sys.stdout, compact=True)`.
- LAN IP: `--lan-host ADDR`가 있으면 그것(소문자로). 없으면 UDP 소켓을 `("192.0.2.1", 9)`에 `connect()`한 뒤 `getsockname()[0]`(패킷은 나가지 않는다).
  기본 경로의 주소이므로 VPN이나 유선·무선이 함께 켜진 노트북에서는 폰이 닿지 않는 주소일 수 있다. 그래서 `--lan-host`를 둔다. 실패하거나 `127.`로
  시작하면 `LAN 주소를 찾지 못했습니다. 같은 네트워크의 기기에서 이 주소로 접속해 보세요: http://<호스트 이름>.local:<port>/?t=<token>`을
  출력하고 QR은 생략한다(IP 주소는 Host 검사를 통과하지 못하므로 안내하지 않는다).
- `--lan` 없이 QR은 출력하지 않는다(폰에서 `127.0.0.1`은 의미가 없다).

## 5. 스키마 (`ms605/gui/schemas.py`)

이 코드가 TS 타입의 유일한 원천이다. 백엔드는 **그대로** 쓰고(docstring·주석 정도만 더할 수 있다), 바꾸려면 이 문서를 먼저 고친다.

```python
"""ms605.gui.schemas -- the GUI wire format. These pydantic models are the single
source of truth for web/src/api/schema.ts (see docs/GUI_API.md section 5)."""

import json
import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, StringConstraints
from pydantic.json_schema import models_json_schema

from ms605.events import LinkState

# -- field types ---------------------------------------------------------------

SiteId = Annotated[str, StringConstraints(min_length=1, max_length=64)]  # any existing id (CLI may have made it)
NewSiteId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")]
DeviceId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{2,128}$")]
Alias = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
SiteName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Location = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Notes = Annotated[str, StringConstraints(max_length=2000)]


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Out(BaseModel):
    # fields with defaults are still always present on the wire -> non-optional in TS
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


# -- requests --------------------------------------------------------------------


class SiteCreate(In):
    name: SiteName
    site_id: NewSiteId | None = None  # None: derived from name (6.3)


class SensorCreate(In):
    device_id: DeviceId
    site_id: SiteId
    alias: Alias
    location: Location = ""
    notes: Notes = ""


class SensorUpdate(In):  # None = leave unchanged
    site_id: SiteId | None = None
    alias: Alias | None = None
    location: Location | None = None
    notes: Notes | None = None


class ReleaseRequest(In):
    device_ids: list[DeviceId] | None = None  # None = every session


class SensorInfoImport(In):
    filename: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    content: Annotated[str, StringConstraints(max_length=65536)]
    site_id: SiteId | None = None  # None: derived from site_name or filename (6.5)
    site_name: SiteName | None = None


# -- views -------------------------------------------------------------------------


class Health(Out):
    status: Literal["ok"] = "ok"
    version: str


class SiteView(Out):
    site_id: str
    name: str


class RegistryInfo(Out):
    site_id: str
    alias: str
    location: str
    notes: str
    last_seen: str | None  # ISO 8601 UTC
    battery_pct: int | None  # as of last_seen


class LiveInfo(Out):
    address: str  # this host's BLE address
    name: str | None  # BLE advertised name
    link: LinkState
    busy: str | None  # "identify" | "read" | "apply" | "calibration" | None
    lost_reason: str
    battery_pct: int | None  # read when identified in this run
    firmware: str | None  # DeviceInfo.version joined with "."
    light_lux: int | None
    gathered_at: float  # epoch s of the latest SensorGathered in this run


class CalibrationSummary(Out):
    timestamp: str  # ISO 8601
    sensitivity: int | None
    detect_mode: int | None


class SnapshotSummary(Out):
    name: str
    taken_at: str  # ISO 8601 UTC
    reason: str


class SensorView(Out):
    device_id: str
    registry: RegistryInfo | None  # None: not registered
    live: LiveInfo | None  # None: no session in this run (never gathered, or released)
    last_calibration: CalibrationSummary | None
    last_snapshot: SnapshotSummary | None


class PendingView(Out):
    site_id: str
    alias: str
    address: str
    source: str  # imported file name


class ConnectingDevice(Out):
    address: str
    since: float  # epoch s


class GatherStatus(Out):
    gathering: bool
    connecting: list[ConnectingDevice]  # new (not yet identified) devices being connected


class SimInfo(Out):
    count: int
    speed: float


class ServerInfo(Out):
    version: str
    lan: bool
    sim: SimInfo | None


class StateSnapshot(Out):
    seq: int
    server: ServerInfo
    gather: GatherStatus
    sites: list[SiteView]
    sensors: list[SensorView]
    pending: list[PendingView]


class ImportResult(Out):
    site_id: str
    added: list[PendingView]


ErrorCode = Literal[
    "unauthorized", "forbidden_origin", "not_found", "already_exists", "busy",
    "not_connected", "invalid", "invalid_request", "invalid_file", "storage", "internal",
]


class ErrorBody(Out):
    code: ErrorCode
    message: str


class ApiError(Out):
    error: ErrorBody


# -- websocket ---------------------------------------------------------------------


class Notice(Out):
    level: Literal["info", "warning", "error"]
    code: Literal["gather_failed", "internal"]
    message: str
    device_id: str | None
    address: str | None
    name: str | None
    at: float  # epoch s


class SensorRemoved(Out):
    device_id: str


class SitesData(Out):
    sites: list[SiteView]


class PendingData(Out):
    pending: list[PendingView]


class SnapshotMessage(Out):
    type: Literal["snapshot"] = "snapshot"
    seq: int
    ts: float
    data: StateSnapshot


class SensorMessage(Out):
    type: Literal["sensor"] = "sensor"
    seq: int
    ts: float
    data: SensorView


class SensorRemovedMessage(Out):
    type: Literal["sensor_removed"] = "sensor_removed"
    seq: int
    ts: float
    data: SensorRemoved


class SitesMessage(Out):
    type: Literal["sites"] = "sites"
    seq: int
    ts: float
    data: SitesData


class PendingMessage(Out):
    type: Literal["pending"] = "pending"
    seq: int
    ts: float
    data: PendingData


class GatherMessage(Out):
    type: Literal["gather"] = "gather"
    seq: int
    ts: float
    data: GatherStatus


class NoticeMessage(Out):
    type: Literal["notice"] = "notice"
    seq: int
    ts: float
    data: Notice


class ServerMessage(
    RootModel[
        Annotated[
            SnapshotMessage | SensorMessage | SensorRemovedMessage | SitesMessage | PendingMessage
            | GatherMessage | NoticeMessage,
            Field(discriminator="type"),
        ]
    ]
):
    pass


REQUEST_MODELS = (SiteCreate, SensorCreate, SensorUpdate, ReleaseRequest, SensorInfoImport)
RESPONSE_MODELS = (Health, StateSnapshot, SiteView, SensorView, ImportResult, ApiError, ServerMessage)


def openapi_document() -> dict:
    """A paths-less OpenAPI 3.1 document holding every wire model (input for openapi-typescript)."""
    _, top = models_json_schema(
        [(m, "validation") for m in REQUEST_MODELS] + [(m, "serialization") for m in RESPONSE_MODELS],
        ref_template="#/components/schemas/{model}",
    )
    return {
        "openapi": "3.1.0",
        "info": {"title": "ms605 gui", "version": "1"},
        "paths": {},
        "components": {"schemas": top.get("$defs", {})},
    }


if __name__ == "__main__":
    text = json.dumps(openapi_document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    Path(sys.argv[1]).write_text(text, encoding="utf-8")
```

규칙:

- 시각 표기는 코어를 따른다. 레지스트리·스냅샷·이력에서 온 값은 ISO 문자열(`last_seen`, `taken_at`, `timestamp`),
  이벤트에서 온 값은 epoch 초 실수(`gathered_at`, `since`, `at`, `ts`)다. 프런트엔드 `format.ts`가 둘 다 받는다.
- 응답은 `model.model_dump(mode="json")` 또는 `model_dump_json()`으로 만든다. WS 메시지는 한 번 직렬화해 모든 클라이언트에 같은 문자열을 보낸다.
- 프런트엔드는 `components["schemas"]["ServerMessage"]`의 `type`으로 좁혀 쓴다. 이 문서를 쓸 때 위 코드를 pydantic 2와
  `openapi-typescript` 7.13으로 실제로 돌려 확인했다: `ServerMessage`는 메시지 7개의 합집합이 되고, 각 멤버의 `type`은 리터럴
  (`"gather"` 등)이며, 기본값이 있는 필드도 필수다. `schema.ts`는 손대지 않는다.

## 6. REST API

모든 경로는 `/api` 아래, JSON(UTF-8)이다. 인증은 `/api/health`만 예외다(4.3절). 모든 핸들러는 **`async def`** 다(8.3절).
변경 API는 성공하면 응답을 돌려주기 **전에** 해당 WS 메시지를 발행한다(`hub.flush()`, 7.4절). 그래서 요청한 클라이언트도 대개 WS로 먼저 바뀐 상태를 받는다.

### 6.1 오류

본문은 항상 `ApiError` 하나다: `{"error": {"code": "...", "message": "..."}}`. `message`는 개발자용 영어(코어 예외 문자열)이고,
화면 문구는 프런트엔드가 `code`로 고른다(10.4절).

| 원인 | 상태 | `code` |
|------|------|--------|
| 토큰 없음·틀림 | 401 | `unauthorized` |
| Origin 불일치 | 403 | `forbidden_origin` |
| 없는 `device_id`/`site_id`/sim index, 없는 `/api` 경로 (핸들러가 조회한 레지스트리의 `KeyError`·없는 세션·sim index, Starlette 404) | 404 | `not_found` |
| 이미 있는 사이트·센서 추가 (해당 핸들러의 `ValueError`) | 409 | `already_exists` |
| `SessionBusyError` (M2에서는 발생하지 않음, M3용) | 409 | `busy` |
| `MS605ConnectionError` (M2에서는 발생하지 않음, M3용) | 409 | `not_connected` |
| 그 밖의 `ValueError`/`ProfileError` | 422 | `invalid` |
| 요청 본문 검증 실패 (`RequestValidationError`) | 422 | `invalid_request` (`message`는 첫 오류의 `loc`과 `msg`) |
| 요청 본문이 512 KiB 초과 (`Content-Length`, 4.3절) | 413 | `invalid_request` |
| 가져오기 파일 형식 오류 (`import_sensor_info`의 `StorageError`) | 422 | `invalid_file` |
| 그 밖의 `StorageError` | 500 | `storage` |
| 예상 밖 예외 (위에서 말한 조회 밖의 `KeyError` 포함: 버그이므로 404로 숨기지 않는다) | 500 | `internal` (로그에 traceback) |
| 그 밖의 Starlette `HTTPException`(405 등) | 원래 상태 | `invalid_request` |

### 6.2 목록

| 메서드·경로 | 요청 | 성공 | 오류 | WS 발행 |
|-------------|------|------|------|---------|
| `GET /api/health` | — | 200 `Health` | — | — |
| `GET /api/state` | — | 200 `StateSnapshot` | 401 | — |
| `POST /api/sites` | `SiteCreate` | 201 `SiteView` | 409, 422 | `sites` |
| `POST /api/sensors` | `SensorCreate` | 201 `SensorView` | 404(사이트), 409, 422 | `sensor` |
| `PATCH /api/sensors/{device_id}` | `SensorUpdate` | 200 `SensorView` | 404(센서·사이트), 422 | `sensor` |
| `DELETE /api/sensors/{device_id}` | — | 204 | 404 | `sensor` 또는 `sensor_removed` |
| `POST /api/gather/start` | — | 200 `GatherStatus` | — | `gather`(바뀌었을 때) |
| `POST /api/gather/stop` | — | 200 `GatherStatus` | — | `gather` |
| `POST /api/release` | `ReleaseRequest` | 204 | 404(세션 없음) | id마다 `sensor` 또는 `sensor_removed`, `gather` |
| `POST /api/import/sensor-info` | `SensorInfoImport` | 200 `ImportResult` | 422(`invalid_file`, `invalid_request`) | `sites`(새 사이트면), `pending` |
| `POST /api/sim/press/{index}` | — | 204 | 404 | (코어 이벤트로) |
| `POST /api/sim/press-all` | — | 204 | — | (코어 이벤트로) |
| `POST /api/sim/drop/{index}` | — | 204 | 404 | (코어 이벤트로) |

`/api/sim/*`는 `--sim`일 때만 등록한다. 아니면 다른 없는 경로처럼 404 `not_found`다.

### 6.3 사이트와 센서

- `POST /api/sites`: `site_id`가 있으면 그대로 `registry.add_site(site_id, name)`(이미 있으면 409). 없으면 `name`에서 만든다:
  소문자로 바꾸고, `[a-z0-9]`가 아닌 글자의 연속을 `-` 하나로 바꾸고, 양끝 `-`를 지우고, 32자로 자른 뒤 다시 끝의 `-`를 지운다.
  비면 `site`. 그 id가 있으면 `-2`, `-3` …을 붙인다(붙인 결과가 32자를 넘지 않도록 앞부분을 자른다). 예: `"Lab A"` → `lab-a`,
  `"3층 사무실"` → `3`, `"회의실"` → `site`. 사이트 이름 바꾸기·삭제는 코어에 없으므로 M2에 없다.
- `POST /api/sensors`: `registry.add_sensor(...)`. 연결 중이 아닌 `device_id`도 받는다(코어와 같다). 두 화면이 같은 센서에
  동시에 이름을 붙이면 나중 요청이 409 `already_exists`를 받는다.
- `PATCH /api/sensors/{device_id}`: `registry.update_sensor(...)`에 `None`이 아닌 필드만 넘긴다. 빈 본문 `{}`도 200이다.
- `DELETE /api/sensors/{device_id}`: `registry.remove_sensor()`. 링크는 건드리지 않는다. 세션이 있으면 그 센서는 "등록되지 않은 센서"로
  남고(`sensor` 발행), 없으면 사라진다(`sensor_removed`).
- 응답 `SensorView`는 7.3절의 뷰 생성 규칙으로 만든다.

### 6.4 모으기와 해제

- `gather/start`: `fleet.start_gather()`(이미 돌고 있으면 아무 일 없음). 응답은 현재 `GatherStatus`.
- `gather/stop`: `await fleet.stop_gather()`(`finish_pending=False`). 그 뒤 허브의 연결 중 목록을 비운다. `finish_pending=True`를
  쓰지 않는 이유: 응답이 최대 연결 기한(10초)만큼 늦어진다. 끝내기 직전에 버튼을 누른 센서는 다시 누르면 된다.
- `release`: `await fleet.release(device_ids)`. 끝나면 id마다 뷰를 다시 만들어 발행한다(등록된 센서는 `live: null`인 `sensor`,
  아니면 `sensor_removed`). 센서의 버튼 창(120초 이상)은 연결이 끊겨도 닫히지 않으므로, 모으기가 돌고 있으면 해제한 센서가 다음 스캔에서
  다시 연결된다. 그래서:
  - `device_ids: null`(전부): 먼저 `await fleet.stop_gather()`하고 허브의 연결 중 목록을 비운 뒤 그때의 세션을 전부 해제한다.
    화면은 모으는 중이면 확인 문구로 이를 알린다(`release.confirmGathering`).
  - id 목록: 세션이 없는 id가 하나라도 있으면 아무것도 닫지 않고 404 `not_found`. 모으는 중이면 그 세션들의 주소를 "이번 모으기에서
    해제한 주소"에 넣는다. `gather/start`는 모으기를 새로 시작할 때 이 집합을 비우고 `fleet.start_gather(accept=...)`로 그 주소를 건너뛴다.
    그래서 해제한 센서는 모으기를 멈췄다 다시 시작해야(그리고 버튼 창이 닫혔으면 버튼을 다시 눌러야) 다시 연결된다.
- 서버를 끝낼 때와 "모두 연결 해제"를 빼면 M2에는 "모으는 중 자동 정지"가 없다. 화면을 떠나도 서버는 계속 모은다(D11: 서버가 기준).

### 6.5 yaml 가져오기

`POST /api/import/sensor-info` 처리:

1. 파일 이름: `Path(filename).name`. 비거나 `.`/`..`이면 `sensor_info.yaml`.
2. 사이트: `site_id`가 있으면 그것(없는 사이트를 새로 만들게 되는 `site_id`는 `NewSiteId` 형식이어야 하고, 아니면 422 `invalid_request`). 없으면 `site_name`이 있으면 거기서, 없으면 파일 이름 stem에서 앞의 `sensor_info_`를 뺀 것에서
   6.3절 규칙으로 slug를 만든다(접미사는 붙이지 않는다. 같은 id의 사이트가 있으면 그 사이트에 넣는다. 같은 파일을 다시 가져와도 되게 하기 위해서다).
   예: `sensor_info_example.yaml` → `example`.
3. `tempfile.TemporaryDirectory()` 안에 1의 이름으로 `content`를 UTF-8로 쓰고 `registry.import_sensor_info(path, site_id, site_name=site_name)`.
   그래서 대기 항목의 `source`가 원래 파일 이름이 된다.
4. `StorageError`면 422 `invalid_file`. `message`에서 임시 경로를 원래 파일 이름으로 바꾼다(예: `sensor_info_example.yaml:3: expected 'name: address', got 'x'`).
   전부 아니면 전무이므로(코어) 아무것도 추가되지 않는다.
5. 응답 `ImportResult(site_id, added)`. 다시 가져와 새 항목이 없으면 `added: []`.

### 6.6 시뮬레이터 제어 (`--sim`일 때만)

`index`는 1부터 `N`까지(`SimFleet.devices[index - 1]`). 범위 밖은 404.

- `press/{index}`: `press_button()`. 연결 창이 열리고, 모으기가 켜져 있으면 다음 스캔에서 연결된다.
- `press-all`: `SimFleet.press_all()`.
- `drop/{index}`: `drop_link()`. 세션이 LOST가 되는 것을 보여 주고 테스트하기 위해서다.

e2e 테스트와 데모가 "사람이 버튼을 누르는" 일을 이것으로 한다. 실기기 모드에서는 라우트가 없다.

### 6.7 M3/M4가 더할 이름 (M2에서는 구현하지 않는다)

모양이 M2와 같은 규칙을 따르도록 이름만 정해 둔다. M2 코드는 이 경로를 만들지 않는다.

| 마일스톤 | 경로·메시지 | 감싸는 코어 |
|----------|-------------|-------------|
| M3 | WS 클라이언트 메시지 `live_subscribe {device_ids}` / `live_unsubscribe {device_ids}` → 서버 메시지 `live_radar`, `live_pir` (7.5절) | `acquire_live()`/`release_live()`(클라이언트 연결별 참조), `LiveRadar`, `PirChanged` |
| M3 | `POST /api/preflight` `{device_ids, window_s?}` → `{results: {device_id: PresenceView}}` | `Fleet.preflight()` |
| M3 | `POST /api/batches` `{device_ids, start_in_s?, start_at?}` → 202 `BatchView` / `POST /api/batches/{batch_id}/cancel` → 202 | `Fleet.calibrate()`, `BatchCalibration.cancel()` |
| M3 | WS `batch`(`BatchChanged`), `SensorView.calibration: CalibrationView \| None` 필드 추가(`CalibrationStateChanged`/`Progress`/`Result`를 접어 넣는다) | 5.2·6.2절 |
| M3 | `GET /api/sensors/{device_id}/history` → `CalibrationRecord[]` | `Storage.read_history()` |
| M4 | `GET /api/sensors/{device_id}/config` → `ConfigProfileView` | `operation("read")` + `read_config()` |
| M4 | `POST /api/apply` `{draft: DraftIn}` → `{results: {device_id: ApplyResultView}}`, WS `apply_result` | `Fleet.apply()`, `ApplyResult` |
| M4 | `GET /api/sensors/{device_id}/snapshots` → `SnapshotSummary[]` / `POST /api/sensors/{device_id}/rollback` `{snapshot}` → `ApplyResultView` | `Storage.list_snapshots()`, `Fleet.rollback()` |

확장 규칙: 필드와 메시지 종류는 **더하기만** 한다. 클라이언트는 모르는 메시지 종류를 무시하되 `seq`는 반영한다(7.6절).

## 7. WebSocket

### 7.1 엔드포인트와 봉투

- 하나뿐이다: `GET /ws`(같은 출처, `ws://` 또는 `wss://` + `location.host`).
- 서버 → 클라이언트는 텍스트 프레임 하나에 JSON 하나, `ServerMessage`(5장)다: `{"type", "seq", "ts", "data"}`.
  - `seq`: 서버 전역 단조 증가 정수(G4). 상태 메시지는 모든 클라이언트에 같은 `seq`로 간다.
  - `ts`: 발행 시각(epoch 초).
  - M3의 일시적 메시지(`live_radar`, `live_pir`)는 `seq: null`이다. 구독한 클라이언트에만 가고 버려도 되기 때문이다.
- 클라이언트 → 서버: M2에는 없다. 서버는 받은 프레임을 읽어 버린다(연결 종료를 알기 위해 계속 읽기만 한다).
  M3부터 `{"type": "...", "data": {...}}` 모양을 쓴다.
- 앱 수준 heartbeat는 없다. uvicorn의 WS ping(기본 20초)이 죽은 연결을 정리한다.

### 7.2 접속 순서

1. 4.3절 검사(실패하면 accept 후 4401/4403으로 닫기).
2. `hub.add_client(ws)`: **같은 동기 구간에서** 스냅샷을 만들고(`seq = hub.seq`) 클라이언트 큐를 등록한다. 둘 사이에 `await`가 없으므로
   스냅샷 이후 발행된 메시지는 빠짐없이, 겹치지 않게 큐에 들어간다.
3. `snapshot` 메시지를 보낸 뒤 큐를 비우며 보낸다(보내기 태스크). 동시에 받기 루프가 연결 종료를 기다린다.
4. 어느 쪽이 끝나면 `hub.remove_client()`.

### 7.3 뷰 만들기

**뷰는 언제나 그 순간의 코어 상태로 새로 만든다. 이벤트는 "무엇을 다시 만들지" 알려 주는 신호일 뿐이다.** 그래서 이벤트의 필드를
뷰에 옮겨 적지 않는다(예외: `gathered_at`, 연결 중 목록, 알림).

`sensor_view(device_id) -> SensorView | None`:

- `registry`: `registry.sensors.get(device_id)`에서. 없으면 `None`.
- `live`: `fleet.sessions.get(device_id)`에서. 없으면 `None`. `address`, `name`, `link=state`, `busy`, `lost_reason=last_lost_reason`,
  `battery_pct`/`firmware`/`light_lux`는 `session.info`에서(`firmware = ".".join(map(str, info.version))`, 없으면 `None`),
  `gathered_at`은 허브가 기록한 마지막 `SensorGathered.at`(없으면 허브가 그 세션을 처음 본 시각).
- `last_calibration`: `storage.read_history(device_id, addresses=<등록된 주소들 + 세션 주소>)`의 마지막 줄에서 `timestamp`,
  `sensitivity`, `detect_mode`. 없으면 `None`. `StorageError`는 경고 로그 후 `None`. 이력 파일은 모든 센서가 함께 쓰고 계속 커지므로
  허브는 파일의 `(st_mtime_ns, st_size)`가 바뀔 때만 `read_history()`로 한 번 읽어 device_id별·주소별 마지막 줄을 색인해 두고 뷰는 그 색인에서 만든다
  (매칭 규칙은 `read_history(device_id, addresses=...)`와 같다).
- `last_snapshot`: `storage.snapshots_dir / device_id`의 `*.json` 중 이름이 가장 큰 것 하나만 `storage.load_snapshot()`으로 읽는다
  (`list_snapshots()`는 전부 읽으므로 쓰지 않는다). 없거나 `StorageError`면 `None`.
- `registry`와 `live`가 둘 다 `None`이면 뷰 없음(`None`) → `sensor_removed`.

`snapshot()`: `sensors`는 (레지스트리 id ∪ 세션 id)의 뷰를 `device_id` 순으로, `sites`는 `site_id` 순, `pending`은 레지스트리 순서 그대로,
`server = ServerInfo(version, lan, sim)`, `gather = gather_status()`.

`gather_status()`: `gathering = fleet.gathering`, `connecting`은 허브가 관리하는 "식별 전 연결 시도" 목록(아래 표)을 `since` 순으로.
`fleet.connecting`(정수)은 쓰지 않는다. 연결 태스크가 끝나 그 수가 줄어드는 시점에는 이벤트가 없어 화면에 늦게 반영되기 때문이다.

`version`: `importlib.metadata.version("ms605-ble")`, 없으면 `"0+unknown"`.

### 7.4 허브: 이벤트 → 메시지

허브는 `fleet.bus.subscribe(callback)` 하나로 이벤트를 받는다(클라이언트마다 `bus.stream()`을 열지 않는다. `seq`를 한곳에서 매기기 위해서다).
콜백은 동기이고 빨라야 한다. 콜백은 허브 상태(연결 중 목록, `gathered_at`, 알림 대기열)를 고치고 "다시 만들 것"(`dirty`)을 표시한 뒤,
아직 예약되지 않았으면 `loop.call_soon(hub.flush)`를 한 번 예약한다. `flush()`는 표시된 것을 다음 순서로 발행한다:
`sites` → `pending` → `sensor`/`sensor_removed`(id 순) → `gather` → `notice`. REST 핸들러는 코어를 바꾼 뒤 해당 항목을 표시하고
응답 전에 `hub.flush()`를 직접 부른다.

이렇게 미루는 이유: 코어는 상태를 바꾸는 도중에 이벤트를 보낸다(예: `release()`는 `close()`의 `LinkStateChanged`를 보낸 뒤에야
`sessions`에서 뺀다). 한 틱 뒤에 뷰를 만들면 한 동작이 끝난 상태를 보내고, 같은 틱의 여러 이벤트가 메시지 하나로 합쳐진다.

| 코어 이벤트 | 허브 처리 | M2 WS |
|-------------|-----------|-------|
| `LinkStateChanged`, `device_id` 있음 | dirty(id) | `sensor` |
| `LinkStateChanged`, `device_id` 없음, `state=CONNECTING` | 연결 중 목록에 `(address, at)` 추가 | `gather` |
| `LinkStateChanged`, `device_id` 없음, `state=CONNECTED` | 그대로 둔다(식별 중) | — |
| `LinkStateChanged`, `device_id` 없음, `DISCONNECTED`/`LOST` | 목록에서 그 주소 제거 | `gather` |
| `BusyChanged` | id가 `sessions`에 있으면 dirty(id) (식별 전의 `"identify"`는 무시) | `sensor` |
| `SensorGathered` | `gathered_at[id] = at`, 목록에서 주소 제거, dirty(id), `resolved_pending`이면 dirty(pending) | `sensor`, `gather`, (`pending`) |
| `GatherFailed` | 목록에서 주소 제거, 알림(`warning`, `gather_failed`, `message=error`, `address`, `device_id`, `name`) | `gather`, `notice` |
| `HandlerFailed` | 알림(`error`, `internal`, `message="<event_type>: <error>"`) | `notice` |
| `KeepAliveMissed`, `FrameDropped` | debug 로그만 | — |
| `LiveRadar`, `PirChanged` | 무시 (M2는 실시간 출력을 켜지 않는다) | — (M3: `live_radar`/`live_pir`) |
| `CalibrationStateChanged`, `CalibrationProgress`, `CalibrationResult`, `BatchChanged`, `ApplyResult` | 무시 (M2에는 이를 일으키는 API가 없다) | — (M3/M4, 6.7절) |

- 이 표는 `ms605.events`의 모든 이벤트 클래스를 덮어야 한다. 허브는 "처리" 집합과 "명시적으로 무시" 집합을 상수로 두고,
  테스트가 둘의 합집합이 `Event`의 모든 하위 클래스(`DeviceEvent` 제외)와 같은지 확인한다(11.1절). M3에서 이벤트가 늘면 이 테스트가 먼저 깨진다.
- 알림 억제: 같은 `(code, address)`의 알림은 10초에 한 번만 발행한다. 버튼 창이 닫힌 센서가 스캔마다 실패해도 화면이 넘치지 않게 한다.
- 허브 콜백이 예외를 내면 버스가 `HandlerFailed`를 보낸다. 허브는 이를 알림으로 바꾸지만, 그 처리에서 또 예외가 나면 버스는 로그만 남긴다(코어 3.3절).

### 7.5 느린 클라이언트와 M3 실시간 메시지

- 클라이언트마다 순서 있는 큐 하나(`maxsize=512`, `create_app(ws_queue_size=...)`로 바꿀 수 있다).
- 발행할 때 어떤 클라이언트의 큐가 가득 차 있으면 그 클라이언트만 정리 대상으로 표시하고, 보내기 태스크가 큐를 버린 뒤 `close(1013)`한다.
  다른 클라이언트에는 영향이 없다. 메시지를 골라 버리지 않는다(G5).
- 한 번의 `send`가 10초를 넘으면(`asyncio.wait_for`) 같은 방식으로 닫는다.
- M3 `live_radar`는 순서 있는 큐에 넣지 않는다. 클라이언트·센서마다 "최신 값 한 칸"을 두고 최대 2 Hz로 내보낸다(최신 값이 이긴다).
  `--speed K`의 시뮬레이터는 tag55를 K Hz로 보내므로 제한이 필요하다. 이 메시지들은 큐 넘침을 일으키지 않는다.

### 7.6 재접속과 재동기화 (클라이언트)

- 메시지 처리: `snapshot`이면 상태를 통째로 바꾸고 `lastSeq = seq`. 그 밖에 `seq`가 정수인 메시지는
  `seq <= lastSeq`면 무시(중복), `seq == lastSeq + 1`이면 적용(모르는 `type`도 `lastSeq`는 올린다), 그보다 크면 **간극**:
  소켓을 닫고 즉시 다시 접속해 새 스냅샷을 받는다. `seq: null`인 메시지(그리고 `seq`가 정수가 아닌 잘못된 메시지)는 순서 검사 없이 적용하고 `lastSeq`는 그대로 둔다.
- 서버가 메시지를 골라 버리지 않으므로 간극은 원칙적으로 생기지 않는다. 그래도 검사한다(서버 버그가 조용한 오류가 되지 않게).
- 닫힘 코드별 동작:

| 코드 | 의미 | 클라이언트 |
|------|------|------------|
| 4401, 4403 | 토큰 없음·틀림, Origin 불일치 | 재접속하지 않고 "접속 권한 없음" 화면 |
| 1013 | 너무 느림(서버가 끊음) | 그 소켓에서 스냅샷을 받았으면 즉시 재접속, 아니면(스냅샷 전송부터 늦음) 아래 백오프 |
| 1001, 1012, 1006 등 | 서버 종료·재시작·네트워크 | 백오프 재접속 |

- 백오프: 0.5, 1, 2, 4, 5, 5 … 초(±20% 무작위). 스냅샷을 받으면 처음부터 다시 센다. `visibilitychange`로 화면이 다시 보이고 소켓이
  열려 있지 않으면 백오프를 기다리지 않고 바로 접속한다(폰이 잠들었다 깨는 경우).
- 끊긴 동안 화면은 마지막 상태를 흐리게(투명도 0.6) 보여 주고 상단 배너를 띄운다. 두 번 연속 실패하면 배너 문구를 "서버에 연결할 수 없습니다"로 바꾼다.
- 서버를 다시 띄우면 토큰이 바뀌므로 재접속은 4401로 끝나고 "접속 권한 없음" 화면이 된다. 이 화면은 "서버가 다시 시작되었을 수 있습니다"를 함께 안내한다.

## 8. 서버 수명주기와 CLI

### 8.1 `ms605 gui`

```
ms605 [--scan-secs S] [--connect-timeout T] gui [--lan [--lan-host ADDR]] [--port 8605] [--sim N [--speed K]]
```

- `ms605/gui/cli.py`:
  - `add_parser(sub) -> None`: `gui` 서브커맨드를 등록한다. 다른 서브커맨드가 전역 옵션을 다루는 방식(`_add_target_args` 등)을 그대로 따른다.
    `--port`: int 0~65535(0 = 빈 포트). `--sim`: int 1~32. `--speed`: float > 0, 기본 1.0, `--sim` 없이 주면 argparse 오류(종료 코드 2). `--lan-host`: 문자열(4.4절), `--lan` 없이 주면 argparse 오류(종료 코드 2).
    `--address`는 쓰지 않는다.
  - `async def run_gui(args, *, scan_secs: float, connect_timeout: float) -> int`.
  - 모듈 최상위에서 fastapi/uvicorn/segno를 import하지 않는다(다른 CLI 명령의 시작 시간을 늘리지 않게). `run_gui` 안에서 import한다.
- `ms605/cli/cli.py`에서 바꾸는 곳은 두 곳뿐이다: `build_parser()`에서 `add_parser(sub)`, `main()`에서
  `elif cmd == "gui": coro = run_gui(args, scan_secs=scan_secs, connect_timeout=ct)`. 이후의 `asyncio.run`, Ctrl-C(130), `os._exit` 흐름을 그대로 탄다.

### 8.2 `run_gui()` 순서

1. `token = secrets.token_urlsafe(32)`.
2. 저장소: `--sim`이면 `Storage(root=data_root() / "cal_results" / "sim")`(G8), 아니면 `Storage()`.
3. `registry = Registry(storage)`. `StorageError`면 stderr에 `레지스트리 파일 오류: <메시지>`, 종료 코드 2(기존 CLI와 같다).
4. Fleet:
   - 실기기: `Fleet(registry, storage, scan_secs=scan_secs, connect_timeout=connect_timeout)`.
   - `--sim N --speed K`: `sim = SimFleet(N, speed=K)`, `Fleet(registry, storage, scan=sim.discover, client_factory=sim.client_factory,
     keepalive_interval=KEEPALIVE_INTERVAL_S / K, scan_secs=scan_secs, connect_timeout=connect_timeout)`.
     시뮬레이터의 유휴 끊김(30초)이 K배 빨라지므로 keep-alive도 같은 비율로 줄인다(코어 14장). 버튼 창(120초)도 120/K초가 된다.
5. 소켓(G9): `socket.socket(AF_INET, SOCK_STREAM)`, `SO_REUSEADDR`, `bind(("0.0.0.0" if lan else "127.0.0.1", port))`, `listen(128)`.
   `OSError`면 `포트 <port>을(를) 열 수 없습니다: <오류>. --port로 다른 포트를 지정하세요.`, 종료 코드 2. 실제 포트는 `getsockname()[1]`.
   listen을 먼저 하므로, 출력 직후의 접속은 uvicorn이 받을 때까지 백로그에서 기다린다.
6. `allowed_hosts`(4.3절), `app = create_app(fleet, registry, storage, token, allowed_hosts=..., sim=sim, lan=lan)`.
7. 4.4절 출력.
8. `server = uvicorn.Server(uvicorn.Config(app, lifespan="on", log_level="warning", access_log=False, timeout_graceful_shutdown=5))`,
   `try: await server.serve(sockets=[sock]) finally: await fleet.aclose()`(이미 닫혔으면 바로 끝난다). 정상 종료는 0이다. Ctrl-C는 uvicorn이 받아 정리한 뒤
   신호를 다시 올릴 수 있으므로 종료 코드는 0 또는 130이다(둘 다 정상).

### 8.3 이벤트 루프 하나

- bleak, 코어, uvicorn, FastAPI 핸들러가 모두 `asyncio.run()`이 만든 **메인 스레드의 루프 하나**에서 돈다(코어 10장, bleak의 CoreBluetooth 백엔드도 이 루프를 쓴다).
- 금지: `uvicorn.run()`(자기 루프를 만든다), uvloop, `workers`/`reload`, 스레드, `run_in_executor`, `asyncio.to_thread`,
  **`def`(동기) 라우트·의존성**. FastAPI는 동기 핸들러를 스레드 풀에서 돌리므로 코어 객체를 다른 스레드에서 건드리게 된다. 모든 핸들러는 `async def`다.
- 레지스트리·저장소의 동기 파일 I/O는 루프에서 그대로 한다(코어 규칙과 같다, 파일이 작다).
- 코어 객체는 생성자에서 루프에 묶이는 것을 만들지 않으므로, `create_app()` 전에 만들어도 된다. `TestClient`는 앱을 별도 스레드의
  루프에서 돌리는데, 그 루프 하나만 코어를 쓰므로 이 규칙에 어긋나지 않는다.

### 8.4 `create_app()`

```python
def create_app(
    fleet: Fleet,
    registry: Registry,
    storage: Storage,
    token: str,
    *,
    allowed_hosts: Sequence[str] = ("127.0.0.1", "localhost"),
    sim: SimFleet | None = None,
    lan: bool = False,
    static_dir: Path | None = None,     # None: 패키지의 static/
    ws_queue_size: int = 512,
) -> FastAPI: ...
```

- `registry is fleet.registry`이고 `storage is fleet.storage`여야 한다. 아니면 `ValueError`.
- `SimInfo(count=len(sim.devices), speed=sim.devices[0].speed)`.
- lifespan: 시작할 때 `hub.attach()`(버스 구독). 끝날 때 열린 WS를 `close(1001)`하고(실제 uvicorn은 lifespan 전에 남은 WS를 먼저 1012로 닫는다. 클라이언트는 둘 다 같은 백오프 재접속으로 처리한다), `await fleet.aclose()`(모으기 중지, 모든 링크 해제 —
  D5), 구독 해제. 그래서 `with TestClient(app):`도 링크를 남기지 않는다. 테스트는 반드시 `with`로 쓴다.
- 정적 파일:
  - 마운트(`StaticFiles`)는 쓰지 않는다. 어느 라우트에도 맞지 않은 요청을 받는 `app.router.default` 대체 처리기 하나가 맡는다.
    `/api/*`이거나 `GET`/`HEAD`가 아니면 404 `ApiError`. 아니면 경로를 `(static_root / 경로).resolve()`로 풀고
    `is_relative_to(static_root)`이고 파일이면 `FileResponse`(경로 탈출 방지). `HEAD`는 `FileResponse`가 본문 없이 답한다.
  - `/assets/*`: 위의 파일 응답에 `Cache-Control: public, max-age=31536000, immutable`(파일 이름에 해시가 있다). 없으면 404.
  - 그 밖의 비API 경로(`/`, `/gather`, `/sensors/...`, `/favicon.svg` …): 정적 디렉터리 안의 파일이면 그 파일, 아니면 `index.html`
    (`Cache-Control: no-store`).
  - `index.html`이 없으면 503 평문 `웹 UI가 빌드되지 않았습니다. cd web && npm ci && npm run build`.
  - `?t=` 교환(4.2절)은 이 경로들에서만 한다.

## 9. 프런트엔드

### 9.1 상태 저장소: Zustand + 순수 리듀서

`store/reducer.ts`의 `reduce(state, message): { state, resync: boolean }`가 모든 상태 변화를 맡고, `store/store.ts`의 Zustand 스토어는
`ws.ts`가 받은 메시지를 이 함수에 넣기만 한다. 고른 이유:

- WS 스트림은 React 바깥에서 들어온다. Zustand는 React 밖에서 `getState()/setState()`로 다룰 수 있고, 컴포넌트는 셀렉터로 필요한 부분만 구독한다
  (센서 하나가 바뀌어도 그 카드만 다시 그린다). 약 1 KB.
- `useSyncExternalStore`를 직접 쓰면 의존성은 없지만, 셀렉터 메모이제이션과 구독 관리를 다시 만들어야 한다. Redux류는 이 규모에 과하다.
- 리듀서가 순수 함수라 DOM 없이 vitest로 검사한다.

```ts
type ConnState = 'connecting' | 'open' | 'reconnecting' | 'unauthorized'
interface AppState {
  conn: ConnState
  failures: number                         // 연속 재접속 실패 수 (배너 문구)
  lastSeq: number | null
  server: ServerInfo | null
  gather: GatherStatus                     // 초기값 { gathering: false, connecting: [] }
  sites: Record<string, SiteView>
  sensors: Record<string, SensorView>
  pending: PendingView[]
  notices: Notice[]                        // 최근 20개
}
```

- 스토어는 WS 메시지로만 바뀐다(G10). REST 응답은 폼의 성공·실패 표시에만 쓴다. 예외: REST 401은 `conn = 'unauthorized'`.
- 폼 입력, 펼친 카드, 다이얼로그 같은 화면 상태는 컴포넌트 `useState`에 둔다.
- 토큰은 JS가 다루지 않는다(HttpOnly 쿠키). `localStorage`는 화면 편의(테마 `ms605.theme`, 마지막 사이트 `ms605.lastSiteId`)에만 쓰고,
  읽기·쓰기를 모두 try/catch로 감싼다.
- 셀렉터(`store.ts`): `selectCounts`(연결됨·끊김·연결 안 됨·전체), `selectUnregistered`(`registry === null && live !== null`),
  `selectBySite`(사이트 이름 → 별명 순, `localeCompare(…, 'ko')`), `selectGathered`(`live !== null`, `gathered_at` 내림차순).

`api/client.ts`: `fetch(path, { method, headers: {'Content-Type': 'application/json'}, body, credentials: 'same-origin' })`.
2xx가 아니면 본문을 `ApiError`로 읽어 `ApiRequestError(status, code, message)`를 던진다(본문이 JSON이 아니면 `code = 'internal'`).
함수 이름: `createSite`, `createSensor`, `updateSensor`, `deleteSensor`, `startGather`, `stopGather`, `release`, `importSensorInfo`,
`simPress`, `simPressAll`, `simDrop`.

### 9.2 라우팅

`wouter`(약 2 KB, 훅 API). history 라우팅이고, 서버가 비API 경로에 `index.html`을 준다(8.4절).

| 경로 | 화면 |
|------|------|
| `/` | 대시보드 |
| `/gather` | 센서 모으기 |
| `/sensors/:deviceId` | 센서 상세(M2는 정보 + 이름·사이트·위치·메모 편집 + 연결 해제 + 등록 삭제. 모니터·보정·설정 자리는 "다음 버전에서 제공됩니다" 안내) |
| 그 밖 | `/`로 이동 |

### 9.3 상태 판정 (`status.ts`)

화면의 모든 상태 표시는 이 함수 하나에서 나온다. 결과는 `{ kind, icon, label, hint? }`이고 `label`은 항상 비어 있지 않다(색만으로 전달하지 않는다).

| 조건 (위에서부터 처음 맞는 것) | kind | 아이콘(lucide) | label | hint |
|-------------------------------|------|----------------|-------|------|
| `live.link === 'connected'`이고 `busy` | `progress` | `Hourglass` | `작업 중 (<busy 문구>)` | — |
| `live.link === 'connected'` | `ok` | `CheckCircle2` | 연결됨 | — |
| `live.link === 'connecting'` | `progress` | `Loader2`(회전) | 연결 중… | — |
| `live.link === 'lost'` | `warn` | `AlertTriangle` | 연결 끊김 | 모으는 중이면 "센서 버튼을 다시 누르세요", 아니면 "센서 모으기를 켜고 버튼을 누르세요" |
| `live.link === 'disconnected'` | `off` | `CircleSlash` | 연결 해제됨 | — |
| `live === null` | `off` | `Clock` | 연결 안 됨 | `registry.last_seen`이 있으면 "마지막 확인 N분 전", 없으면 "확인 기록 없음" |

busy 문구: `identify` 확인 중, `read` 읽는 중, `apply` 설정 적용 중, `calibration` 보정 중, 그 밖은 원문.

### 9.4 컴포넌트

공통 틀:

- `AppShell`: 헤더(제목 `MS605`, `GatherPill`, `LanBadge`, `ThemeToggle`) + 본문 + 내비게이션(폰은 `BottomNav`, 그 이상은 헤더 안 탭).
- `ConnectionBanner`: WS가 열려 있지 않을 때 상단 고정 배너.
- `AuthRequired`: `conn === 'unauthorized'`일 때 전체 화면을 대신한다.
- `GatherPill`: 모으는 중일 때만 헤더에 보인다(`[회전 아이콘] 모으는 중` + 연결 중 수). 누르면 `/gather`. 다른 화면에서도 모으기 상태가 늘 보이게 한다.
- `LanBadge`: `server.lan`이면 `LAN 공개 중`. `server.sim`이면 `시뮬레이터` 배지.
- `LiveRegion`: 시각적으로 숨긴 `aria-live="polite"` 영역(9.7절).
- 공용: `Button`, `StatusBadge`(아이콘 + 문구, kind별 색), `BatteryIndicator`(`Battery`/`BatteryLow` ≤ 20%, 연결 안 됨이면 "(마지막 값)"),
  `RelativeTime`(30초마다 다시 그림), `ConfirmDialog`(네이티브 `<dialog>`, 폰에서는 아래에서 올라오는 시트), `Field`(label + input + 오류 문구).

대시보드:

- `DashboardScreen`
- `SummaryBar`: 연결됨·끊김·연결 안 됨·전체 수(각각 아이콘 + 숫자 + 문구), 세션이 하나라도 있으면 보조 버튼 `모두 연결 해제`(확인 다이얼로그).
- `PrimaryAction`: 이 화면의 유일한 주 동작. 평소 `센서 모으기`(→ `/gather`), 모으는 중이면 `모으는 중 — 보러 가기`.
- `UnregisteredSection`: 등록되지 않은 연결 센서 카드(새 센서 배지, BLE 이름, 상태, `이름 붙이기` → `NameSensorDialog`). 있을 때만 맨 위에.
- `SiteSection` × 사이트: 사이트 이름 + 센서 수, 그 아래 `SensorCard` 격자.
- `SensorCard`: 별명, 위치, `StatusBadge`(+hint), `BatteryIndicator`, "마지막 보정 …" 한 줄. 카드 전체가 `/sensors/:id` 링크.
- `PendingSection`: 가져왔지만 아직 연결되지 않은 항목(별명, 사이트, "버튼을 누르면 자동으로 등록됩니다").
- `EmptyState`: 등록된 센서도 세션도 없을 때. 큰 아이콘 + "아직 등록된 센서가 없습니다" + `센서 모으기`.

센서 모으기:

- `GatherScreen`
- `GatherHero`: 상태별 큰 안내(10.3절). 폰에서 모은 센서가 하나 이상이면 한 줄짜리 축약형으로 줄어 목록과 폼이 엄지 영역으로 올라온다.
- `ConnectingLine`: `센서 N대 연결 중…`(N = `gather.connecting.length`, 0이면 숨김).
- `GatheredList`: `selectGathered` 순. 항목은 `GatheredItem`.
- `GatheredItem`: 등록된 센서면 별명 + `등록됨` + 상태. 등록되지 않았으면 `새 센서` 배지 + BLE 이름 + "이름을 붙여 주세요" + 펼침형 `NameSensorForm`.
- `NameSensorForm`: 이름(별명), `SitePicker`, 위치. 버튼 `나중에`(접기) / `저장`(주). 대시보드의 `NameSensorDialog`도 이 폼을 쓴다.
- `SitePicker`: 기존 사이트 `<select>` + 마지막 항목 `새 사이트…`(고르면 이름 입력칸이 나온다).
- `GatherToggle`: 이 화면의 유일한 주 동작. 폰에서는 하단 탭 바 위에 고정, 높이 64px, 전체 폭. `센서 모으기 시작` / `모으기 끝내기`.
- `ImportSensorInfo`: 접힌 보조 영역 `목록 파일 가져오기 (yaml)`. 파일 선택(`accept=".yaml,.yml,.txt"`), 사이트(`SitePicker`, 비워 두면 파일 이름에서),
  `가져오기` 버튼, 결과 문구.
- `SimPanel`: `server.sim`일 때만. 접힌 영역 `시뮬레이터`: 센서마다 `버튼 누르기 N`, `끊기 N`, 그리고 `모두 누르기`.

센서 상세: `SensorDetailScreen`, `SensorInfoList`(Device ID 전체 고정폭, 주소, 펌웨어, 배터리, 조도, 마지막 확인·보정·설정 백업),
`EditSensorForm`(별명·사이트·위치·메모, `저장`. 사용자가 손대지 않은 칸은 서버의 현재 값을 따라가고 보내지 않는다. `PATCH`에는
바꾼 칸만 싣는다. 다른 화면이 고친 칸을 되돌리지 않기 위해서다. 새 사이트는 `NameSensorForm`과 같이 `ensureSite`), `연결 해제`(세션이 있을 때), `등록 삭제`(위험 색, 확인 다이얼로그), `ComingSoon` 자리.

폼 규칙:

- `NameSensorForm` 기본값: 사이트는 `ms605.lastSiteId`(아직 있으면) → 사이트가 하나뿐이면 그것 → 없으면 `새 사이트…` 모드.
  별명은 `센서 {n}`. n은 그 사이트의 등록 센서 수 + 1부터, 그 사이트에 같은 별명이 없을 때까지 올린다. 위치는 빈칸.
  그래서 걸어 다니며 `저장`만 눌러도 된다.
- 저장: 새 사이트면 `ensureSite(name)`(앞뒤 공백을 빼고 대소문자를 가리지 않아 같은 이름의 사이트가 있으면 그것, 없으면 `createSite({name})`)
  → 받은 `site_id`로 `createSensor(...)`. 사이트를 얻으면 곧바로 폼의 사이트를 그 기존 사이트로 바꾼다. 그래서 `createSensor`가 실패한 뒤
  다시 저장해도 사이트를 또 만들지 않는다. 성공하면 폼을 접는다(바뀐 모습은 WS로 온다).
  409 `already_exists`면 "다른 화면에서 이미 등록했습니다"를 보이고 폼을 접는다.
- 새 센서가 도착하면 가장 최근의 이름 없는 항목 하나만 자동으로 펼친다. 단, 다른 폼에 포커스가 있거나 입력이 남아 있으면 아무것도 펼치거나
  옮기지 않는다. 자동으로 포커스를 주지 않는다(폰 키보드가 갑자기 올라오지 않게). `이름 붙이기`를 직접 눌렀을 때만 이름 칸에 포커스.
- 이름 칸 `enterkeyhint="done"`, Enter로 저장.
- 새로 모은 항목은 5초 동안 배경을 강조한다(`prefers-reduced-motion`이면 강조만, 애니메이션 없음). 방금 누른 센서가 어느 카드인지 알려 주는 유일한 단서다.

### 9.5 반응형

모바일 우선. 좌우 여백 16px, 가로 스크롤 없음, 터치 대상 최소 48px.

| 폭 | 레이아웃 |
|----|----------|
| < 640px (폰) | 한 열. 헤더 56px, 하단 탭 바 64px(`env(safe-area-inset-bottom)` 포함). 모으기 화면의 `GatherToggle`은 탭 바 위에 고정. 다이얼로그는 하단 시트 |
| 640–1023px | 탭이 헤더로 올라가고 하단 탭 바 없음. 카드 2열. 모으기 화면은 가운데 한 열(최대 640px) |
| ≥ 1024px | 본문 최대 1200px. 카드 `repeat(auto-fill, minmax(300px, 1fr))`. 모으기 화면 두 열: 왼쪽(히어로 + 토글 + 가져오기 + 시뮬레이터, sticky), 오른쪽(모은 센서 목록과 폼) |

### 9.6 테마 토큰

`styles/tokens.css`에 `:root`(라이트)로 정의하고, 다크는 `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {…} }`와
`:root[data-theme="dark"] {…}` 두 곳에 같은 값을 둔다. `ThemeToggle`은 시스템 → 밝게 → 어둡게를 돌고 `<html data-theme>`을 바꾼다(시스템이면 속성 제거).
`index.html`에 `<html lang="ko">`, `<meta name="color-scheme" content="light dark">`, `<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">`.

| 토큰 | 라이트 | 다크 | 용도 |
|------|--------|------|------|
| `--bg` | `#f6f7f9` | `#0f1216` | 페이지 배경(`body`에 직접 지정) |
| `--surface` | `#ffffff` | `#171b21` | 카드, 헤더 |
| `--surface-2` | `#eef0f3` | `#1f242c` | 입력칸, 접힌 영역 |
| `--border` | `#d9dde3` | `#2d333c` | 테두리 |
| `--text` | `#1b1f24` | `#e8eaed` | 본문 |
| `--text-muted` | `#5b6470` | `#a0a8b3` | 보조 문구 |
| `--primary` / `--on-primary` | `#2457d6` / `#ffffff` | `#7aa2ff` / `#0f1216` | 주 동작 |
| `--focus` | `#2457d6` | `#7aa2ff` | 포커스 링(2px + 2px offset) |
| `--ok` / `--ok-bg` | `#1a7f37` / `#e6f4ea` | `#4cc26b` / `#13301c` | 연결됨 |
| `--progress` / `--progress-bg` | `#2457d6` / `#e8eefc` | `#7aa2ff` / `#1a2540` | 연결 중, 작업 중 |
| `--warn` / `--warn-bg` | `#b35900` / `#fff1e0` | `#f0a24a` / `#3a2810` | 연결 끊김, 배터리 부족 |
| `--danger` / `--danger-bg` | `#c62828` / `#fdecea` | `#ff6b6b` / `#3b1414` | 삭제, 오류 |
| `--off` / `--off-bg` | `#6b7280` / `#eef0f3` | `#9aa3ad` / `#242a32` | 연결 안 됨, 해제됨 |
| `--radius-sm` / `--radius` | 6px / 12px | 같음 | |
| `--space-1`…`--space-6` | 4 / 8 / 12 / 16 / 24 / 32px | 같음 | |
| `--font-sm` / `--font` / `--font-lg` / `--font-xl` / `--font-hero` | 14 / 16 / 20 / 24 / 32px | 같음 | 글꼴은 시스템 글꼴(`system-ui, -apple-system, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif`) |
| `--tap` | 48px | 같음 | 최소 터치 크기 |

상태 문구 색은 각 `-bg` 위에서 명도 대비 4.5:1 이상이어야 한다(위 값은 그렇게 골랐다. 바꾸면 다시 확인한다).

### 9.7 접근성

- 랜드마크: `header`, `nav`(aria-label "주 메뉴"), `main`. 화면마다 `h1` 하나.
- 상태는 아이콘 + 문구 + 색(9.3절). 아이콘은 `aria-hidden`, 문구가 의미를 전달한다.
- `LiveRegion`이 알린다: 새 센서 "새 센서가 연결되었습니다: <BLE 이름>", 등록된 센서 "<별명> 연결됨", 연결 끊김 "<별명> 연결이 끊겼습니다",
  모으기 시작·끝. 같은 문구를 1초 안에 반복하지 않는다.
- 모든 입력에 `label`. 오류는 `aria-describedby`로 연결. 다이얼로그는 네이티브 `<dialog>`(포커스 가둠, Esc 닫기).
- 포커스 링을 지우지 않는다(`:focus-visible`). `prefers-reduced-motion`이면 회전·맥박 애니메이션을 끈다.

### 9.8 테스트 (vitest)

- `reducer.test.ts`: 스냅샷이 전체를 바꿈, `sensor` upsert, `sensor_removed`, `sites`/`pending`/`gather` 교체, `notice`는 최근 20개만,
  `seq` 중복 무시, 간극이면 `resync: true`, 모르는 `type`도 `lastSeq` 증가.
- `status.test.ts`: 9.3절 표의 각 행, `label`이 항상 비어 있지 않음, LOST의 hint가 모으기 여부에 따라 바뀜.
- `format.test.ts`: 상대 시간(방금 전 / N분 전 / N시간 전 / N일 전 / 7일 넘으면 `YYYY-MM-DD`), ISO 문자열과 epoch 초 둘 다.
- `ws.test.ts`: 가짜 `WebSocket` 클래스로 4401 → `unauthorized`이고 재접속 안 함, 1013 → 스냅샷 뒤면 즉시 재접속·스냅샷 전이면 백오프, `seq` 없는 메시지가 `lastSeq`를 망가뜨리지 않음, 간극 → 닫고 재접속, 백오프 증가와 스냅샷 후 초기화.
- `client.test.ts`: `ApiError` 본문 → `ApiRequestError.code`, 401 → `conn = 'unauthorized'`, JSON 아닌 본문.
- `alias.test.ts`: 기본 별명 `센서 {n}` 규칙.
- 컴포넌트 스모크(@testing-library/react, fixture 스토어): 대시보드가 사이트·센서·미등록·대기 항목을 그림, 빈 상태,
  모으기 화면의 히어로 상태 3가지, 새 센서 폼 저장이 `createSite` → `createSensor`를 순서대로 부름(fetch 모의), 409 문구, `AuthRequired`.
- fixture는 `test/fixtures.ts` 하나에 합성 값으로 둔다(Device ID `53494d3630350001`…, 주소 `02:00:00:00:00:01`…).

## 10. 시각·UX 방향

### 10.1 원칙

1. **화면마다 주 동작은 하나**: 대시보드는 `센서 모으기`, 모으기 화면은 `센서 모으기 시작`/`모으기 끝내기`. 주 동작만 `--primary` 채움 버튼이고,
   나머지(해제, 가져오기, 시뮬레이터)는 테두리 버튼이나 접힌 영역이다.
2. **상태는 늘 보인다**: 색 + 아이콘 + 문구. 모으기 상태는 어느 화면에서든 헤더의 `GatherPill`로 보인다. 서버 연결이 끊기면 배너가 뜬다.
3. **한 손으로 쓰는 모으기 화면**: 폰을 들고 걸어 다니며 버튼을 누르는 상황이다. 주 버튼은 엄지가 닿는 아래쪽에 크게(64px),
   새 센서 폼은 축약된 히어로 바로 아래, 폼의 `저장`은 오른쪽 아래. 기본값만으로 저장할 수 있다(9.4절).
4. **사람이 해야 할 일을 말한다**: "연결 끊김"만 보이지 않고 "센서 버튼을 다시 누르세요"를 함께 보인다.
5. 확인 다이얼로그는 되돌릴 수 없거나 배터리·연결에 영향이 큰 동작(모두 연결 해제, 등록 삭제)에만 쓴다.

### 10.2 문구 (`strings.ts`)

모든 한국어 문구는 `strings.ts` 한곳에 둔다(D15). 키는 영어, 값은 아래 문구 그대로다. `{n}` 같은 자리는 함수로 만든다.

| 키 | 문구 |
|----|------|
| `link.connected` | 연결됨 |
| `link.connecting` | 연결 중… |
| `link.lost` | 연결 끊김 |
| `link.lostHintGathering` | 센서 버튼을 다시 누르세요 |
| `link.lostHintIdle` | 센서 모으기를 켜고 버튼을 누르세요 |
| `link.disconnected` | 연결 해제됨 |
| `link.offline` | 연결 안 됨 |
| `time.lastSeen` | 마지막 확인 {rel} |
| `time.neverSeen` | 확인 기록 없음 |
| `time.justNow` / `time.minutes` / `time.hours` / `time.days` | 방금 전 / {n}분 전 / {n}시간 전 / {n}일 전 |
| `busy.label` | 작업 중 ({reason}) |
| `busy.identify` / `busy.read` / `busy.apply` / `busy.calibration` | 확인 중 / 읽는 중 / 설정 적용 중 / 보정 중 |
| `battery` / `battery.last` | 배터리 {n}% / 배터리 {n}% (마지막 값) |
| `calibration.last` / `calibration.none` | 마지막 보정 {rel} / 보정 기록 없음 |
| `snapshot.last` | 마지막 설정 백업 {rel} |
| `nav.dashboard` / `nav.gather` | 대시보드 / 센서 모으기 |
| `gather.start` / `gather.stop` | 센서 모으기 시작 / 모으기 끝내기 |
| `gather.idleTitle` / `gather.idleBody` | 센서 모으기를 시작하세요 / 시작한 뒤 센서의 버튼을 누르면 여기에 나타납니다 |
| `gather.pressTitle` / `gather.pressBody` | 센서 버튼을 누르세요 / 누른 센서가 아래에 나타납니다 |
| `gather.connecting` | 센서 {n}대 연결 중… |
| `gather.pill` | 모으는 중 |
| `gather.listTitle` | 이번에 모은 센서 ({n}) |
| `sensor.new` / `sensor.nameIt` / `sensor.registered` | 새 센서 / 이름을 붙여 주세요 / 등록됨 |
| `form.alias` / `form.site` / `form.location` / `form.notes` | 이름 / 사이트 / 위치 / 메모 |
| `form.newSite` / `form.newSiteName` / `form.locationPlaceholder` | 새 사이트… / 새 사이트 이름 / 예: 북쪽 벽 |
| `form.save` / `form.later` / `form.nameAction` | 저장 / 나중에 / 이름 붙이기 |
| `form.saved` / `form.takenElsewhere` | 등록했습니다 / 다른 화면에서 이미 등록했습니다 |
| `dash.connectedN` / `dash.lostN` / `dash.offlineN` / `dash.totalN` | 연결됨 {n} / 끊김 {n} / 연결 안 됨 {n} / 전체 {n} |
| `dash.unregistered` / `dash.pending` / `dash.pendingHint` | 등록되지 않은 센서 / 가져온 센서 (아직 연결 안 됨) / 버튼을 누르면 자동으로 등록됩니다 |
| `dash.empty` / `dash.gatheringCta` | 아직 등록된 센서가 없습니다 / 모으는 중 — 보러 가기 |
| `release.all` / `release.one` | 모두 연결 해제 / 연결 해제 |
| `release.confirm` | 연결을 해제하면 배터리를 아낄 수 있습니다. 다시 연결하려면 센서 버튼을 눌러야 합니다. |
| `release.confirmGathering` | 센서 모으기를 멈추고 모든 연결을 해제합니다. 다시 연결하려면 모으기를 시작하고 센서 버튼을 누르세요. (모으는 중일 때의 `release.confirm`) |
| `delete.action` / `delete.confirm` | 등록 삭제 / 이 센서의 이름·사이트·메모를 지웁니다. 센서 설정과 보정 기록은 그대로 남습니다. |
| `import.title` / `import.action` | 목록 파일 가져오기 (yaml) / 가져오기 |
| `import.done` / `import.none` | {n}개를 가져왔습니다. 버튼을 누르면 자동으로 등록됩니다 / 새로 추가된 센서가 없습니다 |
| `conn.lost` / `conn.down` | 서버와 연결이 끊겼습니다. 다시 연결하는 중… / 서버에 연결할 수 없습니다 |
| `auth.title` / `auth.body` | 접속 권한이 없습니다 / 터미널에 표시된 주소나 QR 코드로 다시 접속하세요. 서버가 다시 시작되었다면 새 주소가 필요합니다. |
| `badge.lan` / `badge.sim` | LAN 공개 중 / 시뮬레이터 |
| `sim.title` / `sim.press` / `sim.drop` / `sim.pressAll` | 시뮬레이터 / 버튼 누르기 {n} / 끊기 {n} / 모두 누르기 |
| `detail.comingSoon` | 실시간 모니터·보정·설정 편집은 다음 버전에서 제공됩니다 |
| `detail.notFound` | 센서를 찾을 수 없습니다 |
| `notice.gatherFailed` | 연결 실패: {name} — 버튼을 다시 눌러 주세요 |
| `error.unauthorized` | 접속 권한이 없습니다 |
| `error.forbidden_origin` | 이 주소에서는 요청할 수 없습니다 |
| `error.not_found` | 대상을 찾을 수 없습니다 |
| `error.already_exists` | 이미 등록되어 있습니다 |
| `error.busy` | 센서가 다른 작업 중입니다 |
| `error.not_connected` | 센서가 연결되어 있지 않습니다 |
| `error.invalid` / `error.invalid_request` | 입력값을 확인해 주세요 |
| `error.invalid_file` | 파일 형식이 맞지 않습니다 ({message}) |
| `error.storage` | 파일을 저장하지 못했습니다 |
| `error.internal` | 알 수 없는 오류가 났습니다 |

### 10.3 모으기 화면의 상태

| 상태 | 히어로 | 주 버튼 |
|------|--------|---------|
| 모으지 않음 | `Bluetooth` 아이콘(정지), "센서 모으기를 시작하세요" + 설명 | `센서 모으기 시작` |
| 모으는 중, 이번에 모은 센서 없음 | `BluetoothSearching` 아이콘(맥박), 큰 글씨 "센서 버튼을 누르세요" + "누른 센서가 아래에 나타납니다", `ConnectingLine` | `모으기 끝내기` |
| 모으는 중, 모은 센서 있음 | (폰) 한 줄: `[아이콘] 센서 버튼을 누르세요 · 센서 1대 연결 중…` / (넓은 화면) 위와 같음 | `모으기 끝내기` |

### 10.4 와이어프레임

기호: `[v]` 연결됨 아이콘, `[~]` 진행 중(회전), `[!]` 경고, `[o]` 연결 안 됨(시계), `[*]` 새 센서, `[B]` 배터리, `[BT]` 블루투스.
한글 폭 때문에 세로선은 어긋날 수 있다.

**대시보드 — 폰 (360px)**

```
+----------------------------------+
| MS605        [~ 모으는 중 1]  [◐] |  헤더: 모으는 중일 때만 pill
+----------------------------------+
| [v] 연결됨 3  [!] 끊김 1  전체 7  |  SummaryBar
| +------------------------------+ |
| |        + 센서 모으기          | |  주 동작 (하나뿐)
| +------------------------------+ |
| 등록되지 않은 센서 (1)            |
| +------------------------------+ |
| | [*] 새 센서   MRBL_SIM03      | |
| | [v] 연결됨   [B] 87%          | |
| | ( 이름 붙이기 )               | |
| +------------------------------+ |
| Lab A (4)                        |
| +------------------------------+ |
| | 센서 1              [v] 연결됨 | |
| | 북쪽 벽 · [B] 87%             | |
| | 마지막 보정 3일 전            | |
| +------------------------------+ |
| | 센서 2           [!] 연결 끊김 | |
| | 센서 버튼을 다시 누르세요      | |
| +------------------------------+ |
| | 센서 3         [o] 연결 안 됨  | |
| | 마지막 확인 2시간 전          | |
| | [B] 64% (마지막 값)           | |
| +------------------------------+ |
| 가져온 센서 (아직 연결 안 됨)     |
|  센서 5 · Lab A                  |
|  버튼을 누르면 자동으로 등록됩니다 |
|                                  |
| ( 모두 연결 해제 )                |
+----------------------------------+
|   [=] 대시보드   |  [BT] 센서 모으기 |  하단 탭 바
+----------------------------------+
```

**대시보드 — 데스크톱 (≥1024px)**

```
+-------------------------------------------------------------------------------------------+
| MS605   [대시보드] [센서 모으기]                [~ 모으는 중 · 1대 연결 중] [LAN 공개 중] [◐] |
+-------------------------------------------------------------------------------------------+
|  [v] 연결됨 3   [!] 끊김 1   [o] 연결 안 됨 3   전체 7        ( 모두 연결 해제 )  [+ 센서 모으기] |
|                                                                                           |
|  등록되지 않은 센서 (1)                                                                    |
|  +---------------------------+                                                            |
|  | [*] 새 센서  MRBL_SIM03   |                                                            |
|  | [v] 연결됨   [B] 87%      |                                                            |
|  | ( 이름 붙이기 )           |                                                            |
|  +---------------------------+                                                            |
|  Lab A (4)                                                                                |
|  +---------------------------+ +---------------------------+ +---------------------------+ |
|  | 센서 1         [v] 연결됨 | | 센서 2      [!] 연결 끊김 | | 센서 3     [o] 연결 안 됨 | |
|  | 북쪽 벽 · [B] 87%         | | 센서 버튼을 다시 누르세요 | | 마지막 확인 2시간 전      | |
|  | 마지막 보정 3일 전        | | [B] 80%                   | | [B] 64% (마지막 값)       | |
|  +---------------------------+ +---------------------------+ +---------------------------+ |
|  Lab B (2)                                                                                |
|  +---------------------------+ +---------------------------+                              |
|  | ...                       | | ...                       |                              |
|  +---------------------------+ +---------------------------+                              |
|  가져온 센서 (아직 연결 안 됨): 센서 5 (Lab A) — 버튼을 누르면 자동으로 등록됩니다             |
+-------------------------------------------------------------------------------------------+
```

**센서 모으기 — 폰, 모으지 않음**

```
+----------------------------------+
| MS605                       [◐]  |
+----------------------------------+
|                                  |
|              [BT]                |
|      센서 모으기를 시작하세요     |
|   시작한 뒤 센서의 버튼을 누르면   |
|        여기에 나타납니다          |
|                                  |
| > 목록 파일 가져오기 (yaml)        |  접힌 보조 영역
| > 시뮬레이터                      |  --sim일 때만
|                                  |
| +------------------------------+ |
| |      센서 모으기 시작         | |  64px, 하단 고정
| +------------------------------+ |
|   [=] 대시보드   |  [BT] 센서 모으기 |
+----------------------------------+
```

**센서 모으기 — 폰, 모으는 중(센서 도착 전 → 도착 후)**

```
+----------------------------------+     +----------------------------------+
| MS605        [~ 모으는 중]   [◐] |     | MS605     [~ 모으는 중 1]   [◐]  |
+----------------------------------+     +----------------------------------+
|                                  |     | [BT] 센서 버튼을 누르세요 ·       |
|           (( [BT] ))             |     |      센서 1대 연결 중…            |
|                                  |     |----------------------------------|
|      센서 버튼을 누르세요         |     | 이번에 모은 센서 (3)              |
|   누른 센서가 아래에 나타납니다    |     | +------------------------------+ |
|                                  |     | | [*] 새 센서 · MRBL_SIM03      | |
|      [~] 센서 1대 연결 중…        |     | | 이름을 붙여 주세요            | |
|                                  |     | | 이름   [센서 4            ]   | |
|                                  |     | | 사이트 [Lab A            v]   | |
|                                  |     | | 위치   [예: 북쪽 벽       ]   | |
|                                  |     | | ( 나중에 )        [  저장  ] | |
|                                  |     | +------------------------------+ |
|                                  |     | | 센서 1  [v] 연결됨 · 등록됨   | |
|                                  |     | | 센서 2  [v] 연결됨 · 등록됨   | |
| +------------------------------+ |     | +------------------------------+ |
| |        모으기 끝내기          | |     | |        모으기 끝내기          | |
| +------------------------------+ |     | +------------------------------+ |
|   [=] 대시보드   |  [BT] 센서 모으기 |     |   [=] 대시보드   |  [BT] 센서 모으기 |
+----------------------------------+     +----------------------------------+
```

**센서 모으기 — 데스크톱**

```
+-------------------------------------------------------------------------------------------+
| MS605   [대시보드] [센서 모으기]                              [~ 모으는 중 · 1대 연결 중] [◐] |
+-------------------------------------------------------------------------------------------+
|  +-------------------------------------+   이번에 모은 센서 (3)                             |
|  |              (( [BT] ))             |   +---------------------------------------------+  |
|  |         센서 버튼을 누르세요         |   | [*] 새 센서 · MRBL_SIM03     [v] 연결됨 [B]87% |  |
|  |      누른 센서가 오른쪽에 나타납니다  |   | 이름을 붙여 주세요                            |  |
|  |       [~] 센서 1대 연결 중…          |   | 이름 [센서 4      ] 사이트 [Lab A  v]          |  |
|  |                                     |   | 위치 [예: 북쪽 벽                 ]           |  |
|  |  +-------------------------------+  |   |                     ( 나중에 )  [  저장  ]    |  |
|  |  |        모으기 끝내기           |  |   +---------------------------------------------+  |
|  |  +-------------------------------+  |   | 센서 1   북쪽 벽          [v] 연결됨 · 등록됨  |  |
|  +-------------------------------------+   | 센서 2   창가             [v] 연결됨 · 등록됨  |  |
|  > 목록 파일 가져오기 (yaml)                +---------------------------------------------+  |
|  v 시뮬레이터                                                                              |
|    (버튼 누르기 1) (버튼 누르기 2) (버튼 누르기 3) (모두 누르기)                              |
|    (끊기 1) (끊기 2) (끊기 3)                                                              |
+-------------------------------------------------------------------------------------------+
```

## 11. 테스트

모든 테스트는 시뮬레이터와 `Storage(root=tmp_path)`(또는 `MS605_DATA_DIR=tmp_path`)로 한다. 건너뛰기·xfail·빈 테스트는 두지 않는다.
WS를 쓰는 테스트에는 `@pytest.mark.timeout(30)`을 단다. 코어 테스트처럼 `no_chunk_pacing` 픽스처를 쓴다.

공통 픽스처(테스트 파일 안 또는 `tests/conftest.py`에 추가): `SimFleet(3, speed=100)`, `Fleet(..., scan=sim.discover,
client_factory=sim.client_factory, keepalive_interval=0.15, gather_pause=0.01)`, `create_app(..., token="test-token", sim=sim,
static_dir=<index.html 하나 있는 tmp 디렉터리>)`, `TestClient(app, base_url="http://127.0.0.1:8605")`를 `with`로.
WS 수신은 `recv_until(ws, predicate, limit=200)` 도우미로 조건이 맞는 메시지가 올 때까지 읽고, 읽은 목록을 돌려준다.

### 11.1 백엔드

| 파일 | 꼭 검증할 것 |
|------|--------------|
| `test_gui_auth.py` | 토큰 없는 `/api/state` → 401 `ApiError(unauthorized)`, Bearer → 200, `/?t=test-token` → 303 + `Set-Cookie: ms605_token_8605=…; HttpOnly; SameSite=Lax` + `Location`에 `t` 없음(다른 쿼리 유지), 쿠키로 `/api/state` 200, 틀린 `t` → 303이고 쿠키 없음, 다른 Host → 400, POST에 다른 `Origin` → 403 `forbidden_origin`, 같은 Origin → 통과, `Origin` 없음 → 통과, `/api/health`는 토큰 없이 200, WS 토큰 없음 → 닫힘 4401, WS 다른 Origin → 4403 |
| `test_gui_api.py` | 스냅샷 모양(`seq`, `server.sim.count == 3`, 빈 레지스트리), 사이트 생성의 slug 규칙(`Lab A`→`lab-a`, `회의실`→`site`, 중복 이름 → `-2`), 명시 `site_id` 중복 → 409, 센서 생성·수정·삭제와 404(사이트·센서)·409·422(빈 별명, 모르는 필드), `PATCH {}` → 200, 가져오기(성공, 잘못된 줄 → 422 `invalid_file`이고 메시지에 원래 파일 이름과 줄 번호·임시 경로 없음, 다시 가져오기 → `added: []`, 파일 이름에서 사이트 도출), `release`의 없는 id → 404이고 아무것도 닫히지 않음, `gather/start`·`stop`, 시뮬레이터 아님 → `/api/sim/press/1` 404, 시뮬레이터 범위 밖 index → 404, 없는 `/api` 경로 → 404 `ApiError`, SPA 대체(`/gather` → index.html, `/assets/../..` 탈출 불가), static 없음 → 503 |
| `test_gui_ws.py` | 첫 메시지가 `snapshot`, 그 뒤 `seq`가 1씩 증가, `gather/start` + `sim/press/1` → `gather`(연결 중 1) → `sensor`(`live.link == "connected"`, `registry is None`) → `gather`(연결 중 0), `POST /api/sensors` → 그 센서의 `sensor`에 `registry.alias`, **두 클라이언트가 같은 `(seq, type, data)` 열을 받음**(D11, M2 완료 기준), 늦게 붙은 클라이언트의 스냅샷에 앞의 변경이 반영되고 다음 `seq`가 이어짐, `sim/drop/1` → `live.link == "lost"`, 미등록 센서 `release` → `sensor_removed`, 등록 센서 `release` → `live is None`, 앱 종료 후 모든 시뮬레이터 기기가 연결되지 않음(`not dev.connected`) |
| `test_gui_ws.py` (허브 단위) | 가짜 소켓으로 `Hub`를 직접: 큐가 가득 찬 클라이언트만 1013으로 닫히고 다른 클라이언트는 계속 받음, 같은 `(code, address)` 알림 10초 억제, 한 틱의 여러 이벤트가 `sensor` 하나로 합쳐짐, **이벤트 매핑 표가 `ms605.events`의 모든 이벤트 클래스를 덮음**(7.4절) |
| `test_gui_schema.py` | `web/openapi.json`이 `openapi_document()`를 같은 방식으로 직렬화한 것과 같음(생성 파일이 낡지 않음), `ServerMessage`가 7개 메시지를 `type`으로 구별, 모든 응답 모델 JSON이 `json_schema_serialization_defaults_required`대로 필드를 모두 가짐 |
| `test_gui_e2e.py` | 11.2절 |
| `test_gui_static.py` (프런트엔드 담당) | `ms605/gui/static/index.html`이 있고 그것이 참조하는 `assets/*` 파일이 모두 있음 |

CLI: `--speed`만 주면 argparse 오류(종료 코드 2). 이미 쓰는 포트로 `run_gui()` → 2와 안내 문구. 깨진 `registry.json` → 2와 `레지스트리 파일 오류`.
(`main()`은 `os._exit`하므로 `run_gui()`를 `asyncio.run`으로 직접 부르거나 하위 프로세스로 검사한다.)

### 11.2 e2e 스모크 (`tests/test_gui_e2e.py`)

실제 프로세스·uvicorn·소켓으로 한 번 끝까지 간다. 10초 안팎이어야 한다.

1. `MS605_DATA_DIR=tmp_path`, `PYTHONUNBUFFERED=1`로
   `[sys.executable, "-m", "ms605.cli.cli", "gui", "--sim", "3", "--speed", "20", "--port", "0"]`을 `Popen`(stdout 파이프, stderr 합침).
   읽기 스레드가 줄을 큐에 넣는다.
2. 10초 안에 첫 줄이 `^ms605 gui: (http://127\.0\.0\.1:(\d+)/\?t=([A-Za-z0-9_-]+))$`과 맞아야 한다. 포트와 토큰을 얻는다.
3. `httpx`로 `/api/health` 200을 기다린다(최대 5초). 이후 요청은 `Authorization: Bearer <token>`.
4. `websockets.sync.client.connect("ws://127.0.0.1:<port>/ws", additional_headers=...)`로 클라이언트 **두 개**를 연다. 둘 다 첫 메시지가
   `snapshot`이고 `server.sim == {"count": 3, "speed": 20.0}`, `sensors == []`. 이후 `recv(timeout=…)`로 읽는다.
5. `POST /api/gather/start`, `POST /api/sim/press/1`, `POST /api/sim/press/2` → 두 센서의 `sensor`(`live.link == "connected"`, `registry is None`)를 받는다.
6. `POST /api/sites {"name": "Lab A"}` → `site_id == "lab-a"`. `POST /api/sensors {device_id: <첫 센서>, site_id: "lab-a", alias: "Sensor 1", location: "north wall"}` → 201,
   그 센서의 `sensor`에 `registry.alias == "Sensor 1"`.
7. `tmp_path/cal_results/sim/registry.json`을 읽어 `sites["lab-a"].name == "Lab A"`, `sensors[<id>].alias == "Sensor 1"`, `addresses`에 주소가 있음을 확인한다.
8. `POST /api/sim/drop/2` → 둘째 센서 `live.link == "lost"`.
9. `POST /api/gather/stop`, `POST /api/release {}` → 첫 센서는 `live is None`인 `sensor`, 둘째는 `sensor_removed`.
10. 두 클라이언트가 받은 `(seq, type)` 열이 같고, 각 열의 `seq`가 끊김 없이 이어진다.
11. `SIGINT` → 10초 안에 종료, 종료 코드 0 또는 130.

### 11.3 명령

```
.venv/bin/python -m pytest -q          # 전체 (gui 포함), 60초보다 충분히 짧게
.venv/bin/ruff check .
cd web && npm ci && npm run typegen && npm run typecheck && npm test && npm run build
git status --short web/src/api/schema.ts web/openapi.json ms605/gui/static   # 생성물이 커밋본과 같아야 한다
```

## 12. 통합 순서와 완료 기준

1. (백엔드) `schemas.py`를 5장 그대로, `python -m ms605.gui.schemas web/openapi.json`. 의존성 추가.
2. (병행) 백엔드: `ws.py`, `server.py`, `cli.py`, 테스트. 프런트엔드: 스캐폴드, `npm run typegen`, 스토어·리듀서·화면, vitest.
   프런트엔드는 1이 끝나기 전에는 9장과 5장을 보고 fixture로 작업한다.
3. (통합) `uv run ms605 gui --sim 7`로 실제 화면을 확인하고 `npm run build` 결과를 커밋 대상에 넣는다. `test_gui_static.py` 통과.

M2 완료 기준(GUI_PLAN): `ms605 gui --sim 7 --lan`으로 노트북 브라우저와 폰(QR)에서 동시에 접속해, 한쪽에서 모으기를 시작하고
시뮬레이터 버튼을 누르고 새 센서에 이름을 붙이면 다른 쪽에도 같은 상태가 보인다. 전체 pytest, ruff, `npm run typecheck`, `npm test`, `npm run build`가 통과한다.

## 13. 범위 밖 (M3+)

- 실시간 모니터(막대·PIR·서브센서), WS `live_*` 메시지와 구독(M3)
- 사전점검·일괄 보정·재시도·전후 비교·이력 화면, `batch`/`calibration` 메시지(M3)
- 설정 읽기·초안·차이 미리보기·적용·검증·되돌리기·클론, 임계선 드래그, 상대값 기본 정책(M4)
- 고급 설정(서브센서·DND·시간 동기화)·이력 조회 GUI(M4)
- 사이트 이름 바꾸기·삭제, 대기 항목 삭제, 레지스트리 내보내기 (코어에 API가 없다)
- HTTPS, 여러 사용자 계정·권한, 토큰 고정·재사용(`--token`), 토큰 회전
- 브라우저 자동 열기, 화면 꺼짐 방지(Wake Lock), 진동 피드백, PWA·오프라인
- 다국어(문자열은 `strings.ts` 한곳에 모아 두었다), 데스크톱 패키징(Tauri, D2)
- CLI와 GUI를 동시에 띄울 때의 데이터 디렉터리 잠금(코어 16장과 같다)
