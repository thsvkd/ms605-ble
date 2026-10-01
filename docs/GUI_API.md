# MS605 GUI API (M2, M3)

`docs/GUI_PLAN.md`의 M2(GUI 뼈대)를 구현하기 위한 계약 문서다. 백엔드 구현자(`ms605/gui/*.py`)와
프런트엔드 구현자(`web/**`)는 서로 묻지 않고 이 문서만 보고 동시에 작업한다. 서버는 `docs/CORE_API.md`의
코어 표면(`Fleet`, `Registry`, `Storage`, `EventBus`, `SimFleet`)을 감싸기만 하고, 코어 모듈은 고치지 않는다.
설계 결정 D1~D15를 전제로 하며, 여기서 새로 정한 것은 G 번호와 이유를 함께 적는다.
M3(실시간 모니터·다중 자동 보정)의 계약은 14장이고, M2 본문에서 바뀌는 곳은 14.2절에 모았다.

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

## 14. M3 — 실시간 모니터와 다중 자동 보정

이 장은 M3(GUI_PLAN 4장)의 계약이다. 1~13장(M2)은 그대로 유효하고, 바뀌는 곳은 14.2절에 모두 적었다. 코어 쪽 변경은
`docs/CORE_API.md` 2.2절의 세 가지(`DeviceInfo.zone_distances_m`, `zone_distances()` 인자 타입, `CalibrationJob`의 `"identify"` 대기)뿐이다.
범위: 실시간 모니터(존별 막대·PIR·서브센서 재실), 사전점검 → 즉시/카운트다운/예약 → 진행 → 실패·끊긴 센서만 재시도 → 전후 비교 → 이력 저장,
작업 잠금 표시. 초안 편집·되돌리기·고급 설정·이력 조회 화면은 M4다(14.11절).

### 14.1 새로 정한 것

| # | 주제 | 결정 | 이유 |
|---|------|------|------|
| G11 | 구독 경로 | 실시간 구독은 REST가 아니라 **WS 클라이언트 메시지** `live_subscribe` / `live_unsubscribe`다(M2 6.7절에서 예약한 이름) | 구독의 수명이 WS 연결의 수명과 같다. 연결이 끊기면 서버가 그 클라이언트의 구독을 정확히 지운다. REST로 하면 클라이언트 ID와 임대·만료 시간을 따로 만들어야 하고, 탭을 닫은 화면이 남긴 구독이 센서 배터리를 계속 쓴다 |
| G12 | 기기당 참조 하나 | 서버는 **한 명 이상이 보고 있는 센서마다 `acquire_live()`를 정확히 한 번** 잡고, 보는 클라이언트가 0이 되면 `release_live()`한다. 클라이언트별로 따로 잡지 않는다 | tag54 쓰기가 "보는 사람 0 ↔ 1"에서만 일어난다. 세션 참조 카운트는 그대로 공유되므로 사전점검(`presence_snapshot`)이 같은 센서를 잡아도 맞게 동작한다 |
| G13 | 메시지 하나 | 실시간 값은 `live` 메시지 하나로 보낸다. M2 6.7절이 예약한 `live_radar`/`live_pir` 두 종류는 만들지 않는다 | 센서마다 "최신 값 한 칸"에 tag55와 PIR의 최신 상태가 함께 들어가므로, 중간 프레임을 버려도 PIR 변화를 잃지 않는다. 클라이언트도 한 곳만 갱신한다 |
| G14 | 버려도 되는 메시지 | `live`와 `countdown`은 `seq: null`이고, 순서 있는 큐가 아니라 **클라이언트별·키별 최신 값 칸(slot)** 에 들어간다. 새 값이 칸을 덮어쓴다. 칸은 큐 넘침(1013)을 일으키지 않는다 | 상태 메시지는 하나도 버리면 안 되지만(G5), 실시간 값은 다음 값이 이전 값을 완전히 대신한다. 느린 클라이언트는 프레임을 덜 받을 뿐 끊기지 않는다 |
| G15 | 실시간 주기 | 센서마다 **최대 4 Hz**(`LIVE_INTERVAL_S = 0.25`), 앞 가장자리 즉시 + 뒤 가장자리 보장 | `--speed K` 시뮬레이터는 tag55를 K Hz로 보낸다. 사람 눈에는 4 Hz면 충분하고 7대 × 1 KB × 4 Hz ≈ 28 KB/s다. M2 7.5절의 2 Hz를 바꾼다 |
| G16 | 배치는 서버 상태 | 배치는 **한 번에 하나**이고 서버가 갖는다. 스냅샷의 `batch`에 들어 있어 늦게 붙은 화면도 같은 배치를 보고 조작한다. 끝난 배치는 다음 배치를 만들 때까지 남는다 | 폰에서 시작한 보정이 노트북에도 보여야 한다(D11, M3 완료 기준). 결과·전후 비교를 모든 화면이 볼 수 있어야 한다 |
| G17 | 재시도 = 같은 배치의 새 라운드 | 재시도는 코어 배치를 새로 만들지만(코어 6.2절) GUI에서는 **같은 `batch_id`의 `round + 1`** 이다. 재시도하지 않은 센서의 결과는 그대로 남는다 | 화면 한 장에서 센서 N대의 최종 결과와 전후 비교를 볼 수 있다. 재시도한 센서는 `attempt`가 오른다 |
| G18 | 사전점검 결과 | `POST /api/preflight`의 응답으로 **요청한 화면에만** 준다(G10의 예외: 서버 상태를 바꾸지 않는 조회). 경고를 무시하고 시작했다는 사실만 `BatchView.presence_override`로 남는다 | 사전점검은 시작하려는 사람 한 명의 단계다. 다른 화면에 보낼 서버 상태가 아니다 |
| G19 | 경고 무시 | 재실 경고가 있으면 **"지금" 시작에서만** 명시적 확인(체크박스)을 요구한다. "N초 후"·"시각 예약"에서는 경고를 안내 문구로만 보인다 | 카운트다운·예약은 사람이 방 안에서 시작하고 나가는 흐름이다. 그때의 재실 경고는 당연한 것이고, 매번 확인을 요구하면 경고가 소음이 된다 |
| G20 | 발사 시 모으기 정지 | 라운드가 RUNNING이 되면 서버가 모으기를 멈춘다(`fleet.stop_gather(finish_pending=True)`). 다시 켜는 것은 막지 않는다. 인자가 없는 `stop_gather()`는 재수집 중인 센서의 식별을 취소해 링크를 닫고, 그 센서의 작업이 `LOST`(not connected)로 끝나므로 `finish_pending=True`로 식별을 끝까지 둔다(구현 중 e2e에서 발견) | 코어 10장이 GUI에 넘긴 결정이다. 보정 중 스캔의 영향은 미측정이므로 실기기에서 확인된 CLI 방식(발사 전에 수집 종료)을 따른다. 끊긴 센서를 다시 붙이려면 사람이 모으기를 켠다(재시도 화면이 버튼을 준다) |
| G21 | 진행률 | 진행 막대 = `elapsed_s / expected_s`, LEARNING 동안 최대 99%. `expected_s = EXPECTED_CALIBRATION_S / 시뮬레이터 속도`(실기기 1). 화면에 "예상 시간 기준이며 센서가 알려 주는 진행률이 아님"을 작게 적는다 | D6. 기기는 진행률을 주지 않고 완료(tag62)만 준다. 실제 보정 시간도 미측정이다 |
| G22 | 작업 잠금 | 진행 중인 라운드(WAITING/RUNNING)의 센서는 연결 해제를 거절하고(409 `batch_active`), `busy == "calibration"`인 센서는 사전점검과 새 배치를 거절한다(409 `busy`). `"identify"`는 잠금으로 보지 않는다(코어가 2초까지 기다린다) | D11. 보정 중 링크가 끊기면 학습이 초기화된다. `"identify"`는 사람이 건 작업이 아니라 재수집 직후 잠깐 잡히는 읽기다 |
| G23 | 존 거리 | `live`의 `distance_m`은 `models.zone_distances(session.info)`(tag53, 식별할 때 함께 읽음. 코어 2.2절). 세션 정보가 없으면 `FALLBACK_DISTANCES_M` | 실시간 경로에서 설정 읽기 잠금(`"read"`)을 잡지 않는다. 잡으면 그 순간 발사된 배치가 실패한다 |

### 14.2 M2 본문에서 바뀌는 것

| 위치 | M2 | M3 |
|------|----|----|
| 5장 | `ServerMessage` 7종, `StateSnapshot`에 `batch` 없음, `ErrorCode` 11개 | 14.4절대로 바꾼다: 메시지 11종, `StateSnapshot.batch`, `ErrorCode`에 `batch_active`, 요청 모델·`ClientMessage` 추가 |
| 6.1절 | `busy`, `not_connected`는 M2에서 발생하지 않음 | 발생한다(14.5절). 새 행: 진행 중인 배치와 겹침 → 409 `batch_active` |
| 6.2·6.4절 `POST /api/release` | 항상 허용 | 진행 중인 라운드의 센서가 들어 있으면 409 `batch_active`(14.5.6절) |
| 6.7절 | `live_radar`/`live_pir`, `SensorView.calibration`, `GET /api/sensors/{id}/history`(M3) | `live` 하나(G13). `SensorView.calibration`은 만들지 않는다(보정 상태는 `batch`에 있다, 센서의 잠금은 이미 `live.busy`로 보인다). 이력 조회는 M4로 옮긴다 |
| 7.1절 | 클라이언트 → 서버 메시지 없음, 일시적 메시지는 `live_radar`/`live_pir` | `ClientMessage`(14.6.1절). 일시적 메시지는 `live`, `countdown` |
| 7.4절 표 | `LiveRadar`, `PirChanged`, `Calibration*`, `BatchChanged`는 무시 | 처리한다(14.6.6절). `ApplyResult`만 무시(M4) |
| 7.5절 | `live_radar` 최대 2 Hz | `live` 최대 4 Hz, 슬롯 규칙은 14.6.3절 |
| 9.2절 라우트 | `/`, `/gather`, `/sensors/:id` | `/monitor`, `/calibrate` 추가. 센서 상세에 실시간 띠와 "이 센서 보정" 링크 |
| 9.4절 `AppShell` | 탭 2개 | 탭 4개(대시보드·센서 모으기·모니터·보정), 헤더에 `CalibrationPill` |
| 10.2절 | `detail.comingSoon` = "실시간 모니터·보정·설정 편집은 다음 버전에서 제공됩니다" | "설정 편집은 다음 버전에서 제공됩니다". 14.8.10절의 키를 더한다 |
| 11.1절 `test_gui_schema.py` | `ServerMessage` 7개 | 11개 |
| 11.1절 `test_gui_ws.py` | `seq`가 1씩 증가 | `seq`가 정수인 메시지만 센다(`live`, `countdown`은 `seq: null`). 기존 도우미 `_assert_consecutive`에 넘기기 전에 거른다 |
| 13장 | 실시간 모니터·보정은 범위 밖 | 14장에서 다룬다 |

### 14.3 파일과 소유권

```
ms605/gui/
  schemas.py   (+14.4절)
  live.py      새 파일. LiveFeed: 구독 집합, 기기당 acquire_live 하나, 4 Hz 조절, live 메시지
  batch.py     새 파일. BatchService: 현재 배치(라운드들), 생성·재시도·취소, BatchView/CalibrationJobView, 카운트다운, 발사 시 모으기 정지
  ws.py        Client에 슬롯, Hub가 LiveFeed·BatchService에 이벤트를 넘기고 batch/calibration_job을 발행, 받기 루프가 ClientMessage 처리
  server.py    14.5절 라우트, release 거절 규칙
ms605/session.py, ms605/models.py, ms605/calibration.py   코어 2.2절 (작은 변경 세 가지)
web/src/
  meter.ts                     CLI 막대의 포팅 (14.8.4절)
  calibration.ts               작업·배치 상태 → 문구·아이콘·진행률 (14.8.9절)
  api/client.ts, api/ws.ts, api/types.ts, store/reducer.ts, store/store.ts   (+14.8.2~3절)
  hooks/useLiveWatch.ts, hooks/useCountdown.ts
  screens/Monitor.tsx, screens/Calibrate.tsx, (screens/SensorDetail.tsx 수정)
  components/Meter.tsx, ZoneMeterRow.tsx, SensorLiveCard.tsx, LiveStrip.tsx, PresenceChips.tsx, SensorPicker.tsx, CalibrationPill.tsx,
  components/calibrate/SelectStep.tsx, PreflightStep.tsx, StartOptions.tsx, BatchProgress.tsx, JobRow.tsx, BatchResults.tsx, RetryPanel.tsx, ThresholdCompare.tsx
  test/meter_cases.json        CLI와 같이 쓰는 막대 기준 사례 (14.8.4절)
tests/
  test_gui_live.py, test_gui_batch.py, test_gui_meter_parity.py   새 파일
  test_gui_ws.py, test_gui_schema.py, test_gui_e2e.py             수정
  test_calibration.py, test_session.py, test_fleet.py             코어 2.2절 회귀 (CORE_API 14장 표)
```

| 소유 | 파일 | 비고 |
|------|------|------|
| 코어 | `ms605/session.py`, `ms605/models.py`, `ms605/calibration.py`, `tests/test_{calibration,session,fleet}.py` | 가장 먼저 한다. 백엔드의 `distance_m`과 재수집 직후 발사가 이것에 기댄다 |
| 백엔드 | `ms605/gui/*.py`, `web/openapi.json`, `tests/test_gui_{live,batch,meter_parity,ws,schema,e2e}.py` | `schemas.py`를 14.4절 그대로 고치고 `web/openapi.json`을 다시 만든다 |
| 프런트엔드 | `web/**`(`openapi.json` 제외), `ms605/gui/static/**`, `web/src/test/meter_cases.json` | `meter_cases.json`은 14.8.4절의 표 그대로다. 파이썬 테스트도 이 파일을 읽는다 |

### 14.4 스키마 추가 (`ms605/gui/schemas.py`)

아래 다섯 군데를 **그대로** 고친다. 이 문서를 쓸 때 5장의 코드에 이 변경을 적용해 pydantic 2와 `openapi-typescript` 7.13으로 돌려
확인했다: `ruff check` 통과, `ServerMessage`가 11개 메시지의 합집합이고 `live`/`countdown`의 `seq`는 TS에서 `null`, `ClientMessage`는
두 메시지의 합집합, `BatchStart` 검증기의 오류(`delay_s`/`at` 누락·과잉, 시간대 없는 `at`, 중복 `device_ids`, 모르는 필드)가
`RequestValidationError`가 된다.

**(1) import** — 맨 위의 pydantic import 두 줄과 `ms605.events` import를 바꾼다:

```python
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel, StringConstraints, model_validator
from pydantic.json_schema import models_json_schema

from ms605.events import BatchState, CalibrationState, LinkState
```

**(2) 새 모델** — `class StateSnapshot(Out):` 바로 앞에 넣는다:

```python
# -- M3: live monitor ------------------------------------------------------------------


class LiveWatch(In):
    device_ids: Annotated[list[DeviceId], Field(max_length=32)]


class LiveSubscribeMessage(In):  # client -> server
    type: Literal["live_subscribe"]
    data: LiveWatch


class LiveUnsubscribeMessage(In):  # client -> server
    type: Literal["live_unsubscribe"]
    data: LiveWatch


class ClientMessage(
    RootModel[Annotated[LiveSubscribeMessage | LiveUnsubscribeMessage, Field(discriminator="type")]]
):
    pass


class LiveZone(Out):
    index: int  # 0..6
    distance_m: float  # far edge, models.zone_distances()
    enabled: bool
    trigger_active: bool  # the device's own flag (colours the trigger cur/thr text, as the CLI)
    trigger: int  # current_trigger
    trigger_threshold: int  # signed: calibration can make it zero or negative
    maintain: int  # current_maintain
    maintain_threshold: int


class LiveData(Out):
    device_id: str
    at: float  # epoch s of the tag55 push behind this frame
    pir: bool | None  # session.last_pir; None: not seen in this stream yet
    sub_sensor_presence: list[bool]  # S1..S3, the device's own presence call
    zones: list[LiveZone]


class Countdown(Out):
    batch_id: str
    fire_at: float  # epoch s (server clock)
    remaining_s: float  # fire_at - ts, never below 0


# -- M3: preflight and batch calibration -------------------------------------------------


class PreflightRequest(In):
    device_ids: Annotated[list[DeviceId], Field(min_length=1, max_length=32)]
    window_s: Annotated[float, Field(ge=0.2, le=10.0)] = 3.0  # calibration.PREFLIGHT_WINDOW_S


class PresenceView(Out):
    device_id: str
    samples: int  # tag55 pushes seen in the window
    presence: bool | None  # any sub-sensor presence; None: no sample
    pir: bool | None  # PIR seen detected; None: no PIR value seen
    occupied: bool | None  # presence or pir; None: neither seen
    error: str | None  # e.g. "not connected"


class PreflightResult(Out):
    checked_at: float  # epoch s
    results: list[PresenceView]  # request order


StartMode = Literal["now", "delay", "at"]


class BatchStart(In):
    start: StartMode = "now"
    delay_s: Annotated[int, Field(ge=1, le=3600)] | None = None  # required iff start == "delay"
    at: AwareDatetime | None = None  # required iff start == "at"; ISO 8601 with an offset

    @model_validator(mode="after")
    def _check_start(self) -> "BatchStart":
        if (self.start == "delay") != (self.delay_s is not None):
            raise ValueError("delay_s is required with start='delay', and only then")
        if (self.start == "at") != (self.at is not None):
            raise ValueError("at is required with start='at', and only then")
        return self


class BatchCreate(BatchStart):
    device_ids: Annotated[list[DeviceId], Field(min_length=1, max_length=32)]
    presence_override: bool = False  # the operator started despite an occupied preflight

    @model_validator(mode="after")
    def _check_ids(self) -> "BatchCreate":
        if len(set(self.device_ids)) != len(self.device_ids):
            raise ValueError("device_ids has duplicates")
        return self


class BatchRetry(BatchStart):
    device_ids: Annotated[list[DeviceId], Field(min_length=1, max_length=32)] | None = None  # None: every retryable


class ZonePair(Out):
    trigger: int
    maintain: int


class CalibrationJobView(Out):
    batch_id: str
    device_id: str
    attempt: int  # 1, +1 each time a retry includes this sensor
    state: CalibrationState  # "idle": waiting for the fire time
    started: bool  # tag52=4 was sent (lost + started: dropped while learning)
    elapsed_s: float | None  # since LEARNING began, last CalibrationProgress; None before LEARNING
    error: str | None
    detail: str
    before: list[ZonePair] | None  # tag51 just before STARTING
    after: list[ZonePair] | None  # tag51 read back after SUCCEEDED
    history_saved: bool
    retryable: bool  # state is failed, lost or timeout


class BatchView(Out):
    batch_id: str  # stable across retries (the first round's core batch id)
    state: BatchState  # of the current round
    round: int  # 1, +1 per retry
    start: StartMode  # of the current round
    fire_at: float  # epoch s, current round
    created_at: float  # epoch s, first round
    expected_s: float  # progress denominator: EXPECTED_CALIBRATION_S / sim speed (not device progress)
    presence_override: bool
    device_ids: list[str]  # every sensor of the batch, selection order
    round_ids: list[str]  # the sensors of the current round
    jobs: list[CalibrationJobView]  # device_ids order
```

**(3) `StateSnapshot`** — `pending` 다음에 한 줄:

```python
    batch: BatchView | None  # the current or last batch, kept until the next one (late joiners)
```

**(4) `ErrorCode`** — 마지막에 `"batch_active"`를 더한다:

```python
ErrorCode = Literal[
    "unauthorized", "forbidden_origin", "not_found", "already_exists", "busy",
    "not_connected", "invalid", "invalid_request", "invalid_file", "storage", "internal", "batch_active",
]
```

**(5) WS 메시지와 모델 목록** — `class ServerMessage(`부터 `RESPONSE_MODELS = ...` 줄까지를 아래로 바꾼다(`NoticeMessage`까지는 그대로):

```python
class BatchMessage(Out):
    type: Literal["batch"] = "batch"
    seq: int
    ts: float
    data: BatchView


class CalibrationJobMessage(Out):
    type: Literal["calibration_job"] = "calibration_job"
    seq: int
    ts: float
    data: CalibrationJobView


class LiveMessage(Out):  # transient: no seq, only to subscribers, coalesced per sensor
    type: Literal["live"] = "live"
    seq: None = None
    ts: float
    data: LiveData


class CountdownMessage(Out):  # transient: no seq, 1 Hz while a batch waits
    type: Literal["countdown"] = "countdown"
    seq: None = None
    ts: float
    data: Countdown


class ServerMessage(
    RootModel[
        Annotated[
            SnapshotMessage | SensorMessage | SensorRemovedMessage | SitesMessage | PendingMessage
            | GatherMessage | NoticeMessage | BatchMessage | CalibrationJobMessage | LiveMessage | CountdownMessage,
            Field(discriminator="type"),
        ]
    ]
):
    pass


REQUEST_MODELS = (
    SiteCreate, SensorCreate, SensorUpdate, ReleaseRequest, SensorInfoImport,
    PreflightRequest, BatchCreate, BatchRetry, ClientMessage,
)
RESPONSE_MODELS = (
    Health, StateSnapshot, SiteView, SensorView, ImportResult, ApiError, ServerMessage,
    PreflightResult, BatchView,
)
```

`api/types.ts`에 별칭을 더한다: `LiveData`, `LiveZone`, `Countdown`, `PresenceView`, `PreflightResult`, `BatchView`, `CalibrationJobView`,
`ZonePair`, `CalibrationState`, `BatchState`, `ClientMessage`, `PreflightRequest`, `BatchCreate`, `BatchRetry`, `StartMode = BatchCreate['start']`.

### 14.5 REST

#### 14.5.1 목록

| 메서드·경로 | 요청 | 성공 | 오류 | WS 발행 |
|-------------|------|------|------|---------|
| `POST /api/preflight` | `PreflightRequest` | 200 `PreflightResult` | 404(세션 없음), 409 `busy`, 422 | — (G18) |
| `POST /api/batches` | `BatchCreate` | 202 `BatchView` | 404, 409 `batch_active`/`not_connected`/`busy`, 422 | `batch` |
| `GET /api/batches/{batch_id}` | — | 200 `BatchView` | 404 | — |
| `POST /api/batches/{batch_id}/cancel` | — | 200 `BatchView` | 404 | `batch`, `calibration_job` |
| `POST /api/batches/{batch_id}/retry` | `BatchRetry` | 202 `BatchView` | 404, 409 `batch_active`/`not_connected`/`busy`, 422 | `batch` |

`batch_id`는 서버가 가진 배치(현재 또는 마지막) 하나의 id만 맞는다. 다른 id는 404 `not_found`다. 배치의 현재 상태는
`GET /api/state`의 `batch`에도 있다. 모든 핸들러는 `async def`이고, 코어를 바꾼 뒤 응답 전에 `hub.flush()`한다(6장 규칙).

#### 14.5.2 사전점검

1. `device_ids` 중 `fleet.sessions`에 없는 것이 있으면 아무 I/O 없이 404 `not_found`(`message`에 id 목록).
2. 세션의 `busy == "calibration"`이거나 진행 중인 라운드(14.6.5절 `members()`)에 든 센서가 있으면 409 `busy`.
3. `snapshots = await fleet.preflight(device_ids, window_s=window_s)`. 연결이 안 된 센서는 코어가 `error="not connected"`인 결과로 준다(예외 아님).
4. `PreflightResult(checked_at=time.time(), results=[PresenceView(device_id=i, **필드) for i in device_ids])`.

응답 시간은 `window_s`(기본 3초)다. 화면은 그동안 진행 표시를 한다.

#### 14.5.3 배치 만들기

`POST /api/batches` 처리 순서(앞에서 걸리면 거기서 끝나고, 아무것도 바뀌지 않는다):

1. 진행 중인 배치(현재 라운드가 WAITING/RUNNING)가 있으면 409 `batch_active`.
2. 세션이 없는 id → 404 `not_found`.
3. `session.state`가 CONNECTED가 아닌 id → 409 `not_connected`.
4. `session.busy`가 `None`도 `"identify"`도 아닌 id → 409 `busy`(`message`에 `<id>: <busy>`).
5. 시작 시각:
   - `now` → `start=0.0`
   - `delay` → `start=float(delay_s)` (사람 시간이므로 시뮬레이터 속도로 나누지 않는다)
   - `at` → `start=body.at`(시간대가 있는 `datetime`. 코어가 `.timestamp()`로 바꾼다). `body.at.timestamp() - time.time() > MAX_SCHEDULE_AHEAD_S`(86400)이면 422 `invalid`
     ("at is more than 24 h ahead"). 지난 시각은 코어의 `ValueError` → 422 `invalid`.
6. `core = fleet.calibrate(device_ids, start=start, timeout=CALIBRATION_TIMEOUT_S / speed)`. `speed`는 `--sim`의 속도, 실기기는 1.
7. 서버의 배치를 새로 만든다(이전 배치를 버린다): `batch_id = core.batch_id`, `round = 1`, `start` 방식, `created_at = time.time()`,
   `presence_override`, `device_ids`, 모든 센서 `attempt = 1`. 카운트다운 태스크를 시작한다(14.6.5절).
8. `batch`를 표시하고 `hub.flush()`, 202 `BatchView`.

`at`의 "HH:MM → 오늘 또는 내일" 해석은 화면이 한다(14.8.8절). 서버는 시간대가 있는 시각만 받는다.

#### 14.5.4 재시도

`POST /api/batches/{batch_id}/retry`:

1. 서버 배치가 없거나 id가 다르면 404. 현재 라운드가 WAITING/RUNNING이면 409 `batch_active`.
2. 후보 = `device_ids` 중 마지막 결과가 FAILED·LOST·TIMEOUT인 센서(`CalibrationJobView.retryable`, 코어 `retry_ids()`와 같은 기준).
3. 대상:
   - `body.device_ids is None` → 후보 중 세션이 있고 CONNECTED인 것 전부. 하나도 없으면 409 `not_connected`("no retryable sensor is connected").
   - 목록 → 후보가 아닌 id가 있으면 422 `invalid`, 세션이 없으면 404, CONNECTED가 아니면 409 `not_connected`.
4. 14.5.3절의 4~6단계(잠금 검사, 시작 시각, `fleet.calibrate`).
5. 라운드를 올린다: `round += 1`, 현재 코어 배치 = 새 배치, `round_ids = 대상`, 대상의 `attempt += 1`과 경과 시간 초기화, `start` 방식 갱신.
   대상이 아닌 센서의 결과는 그대로 남는다(G17). 카운트다운 시작, `batch` 발행, 202 `BatchView`.

`presence_override`는 바꾸지 않는다. 재시도 화면은 사전점검을 다시 하지 않는다(14.8.6절 `RetryPanel`).

#### 14.5.5 취소와 조회

- `POST /api/batches/{batch_id}/cancel`: id가 다르면 404. 현재 라운드가 이미 끝났으면 아무것도 하지 않고 200(멱등). 아니면
  `await core.cancel()`(WAITING: 기기 I/O 없음, RUNNING: 링크를 끊어 학습을 멈춤, 코어 5.3절)이 끝난 뒤 200 `BatchView`.
  RUNNING 취소는 링크 해제까지 기다리므로 최대 `RELEASE_TIMEOUT_S`(5초) 걸릴 수 있다.
  재시도 라운드(`round > 1`)를 취소했을 때 `cancelled`·`started: false`로 끝난 센서(카운트다운 중 취소처럼 tag52를 보내지 않은
  센서)는 그 시도가 없었던 것으로 본다. 그 센서의 `core_for`·`attempt`·경과 시간을 재시도 전 값으로 되돌리므로 앞선 FAILED·LOST·TIMEOUT
  결과와 `retryable: true`가 다시 보이고 `/retry`를 그대로 쓸 수 있다. 배치의 `state`(`cancelled`)·`round`·`round_ids`는 취소된 라운드의 값이다.
- `GET /api/batches/{batch_id}`: 200 `BatchView` 또는 404.

#### 14.5.6 작업 잠금: M2 경로에 더하는 거절 (G22)

| 경로 | 조건 | 응답 |
|------|------|------|
| `POST /api/release` `{device_ids: null}` | 진행 중인 라운드가 있음 | 409 `batch_active`, 아무것도 하지 않음(모으기도 멈추지 않음) |
| `POST /api/release` `{device_ids: [...]}` | 진행 중인 라운드의 센서가 하나라도 있음 | 409 `batch_active`, 아무것도 닫지 않음 |
| `POST /api/preflight` | 14.5.2절 2단계 | 409 `busy` |
| `POST /api/batches`, `/retry` | 14.5.3절 1·4단계 | 409 `batch_active` / `busy` |

`POST /api/release`, `POST /api/batches`, `/retry`는 서버의 `asyncio.Lock` 하나로 차례로 처리한다. 검사는 잠금을 잡은 뒤에 한다.
그래서 release가 `stop_gather()`나 연결 취소를 기다리는 동안 들어온 배치 생성은 release가 끝난 뒤에 검사되고(닫힌 센서 → 404),
검사를 통과한 배치의 센서를 뒤이은 release가 닫는 일이 없다.

`gather/start`·`gather/stop`, `DELETE /api/sensors/{id}`(레지스트리만), `/api/sim/*`는 막지 않는다. `sim/drop`은 보정 중 끊김을 재현하는 수단이다.

### 14.6 WebSocket

#### 14.6.1 클라이언트 → 서버 (G11)

텍스트 프레임 하나에 `ClientMessage` 하나:

```json
{"type": "live_subscribe", "data": {"device_ids": ["53494d3630350001", "53494d3630350002"]}}
{"type": "live_unsubscribe", "data": {"device_ids": ["53494d3630350002"]}}
```

- 구독은 **연결마다의 집합**이다. `live_subscribe`는 합집합, `live_unsubscribe`는 차집합이고 둘 다 멱등이다(같은 id를 두 번 구독해도 한 번).
  화면 안의 참조 카운트는 클라이언트가 한다(14.8.3절).
- 세션이 없는 id도 받는다(구독 의사). 그 센서가 모이면 그때 실시간 출력을 켠다. 모니터 화면을 먼저 열어 두고 센서를 모으는 흐름이 된다.
- 연결이 끊기면 서버는 그 연결의 구독을 모두 지운다. 그래서 클라이언트는 **스냅샷을 받을 때마다** 지금 보고 있는 id 전체를 다시 구독한다.
- 4096바이트를 넘거나, 바이너리이거나, `ClientMessage`로 검증되지 않는 프레임은 경고 로그만 남기고 버린다. 연결은 유지한다
  (우리 클라이언트의 버그로 재접속 고리가 생기지 않게).
- 받기 루프(M2의 `_drain`을 대신한다)는 메시지를 차례로 `LiveFeed.subscribe/unsubscribe`에 넘긴다. 둘 다 동기 함수이고 기기 I/O는
  백그라운드 태스크가 한다(14.6.4절). 그래서 느린 tag54 쓰기가 받기 루프를 막지 않는다.

#### 14.6.2 서버 → 클라이언트 새 메시지

| `type` | `seq` | 받는 쪽 | 언제 | `data` |
|--------|-------|---------|------|--------|
| `batch` | 정수 | 모두 | 배치 생성, 재시도(라운드 시작), 라운드 상태 전이(`BatchChanged`), 취소 | `BatchView` 전체 |
| `calibration_job` | 정수 | 모두 | 현재 라운드 센서의 `CalibrationStateChanged`, `CalibrationProgress`(약 1초마다), `CalibrationResult` | `CalibrationJobView` 하나 |
| `live` | `null` | 그 센서를 구독한 연결만 | tag55/tag56 push, 센서마다 최대 4 Hz | `LiveData` |
| `countdown` | `null` | 모두 | 현재 라운드가 WAITING인 동안 1초마다 | `Countdown` |

- 같은 `flush()`에서 `batch`를 보내면 그 틱의 `calibration_job`은 보내지 않는다(`batch`가 모든 작업을 담는다).
- `flush()` 순서: `sites` → `pending` → `sensor`/`sensor_removed` → `batch` → `calibration_job`(id 순) → `gather` → `notice`.
- `calibration_job`의 `batch_id`가 클라이언트가 가진 배치와 다르면 클라이언트는 무시한다(`seq`는 반영한다).
- `live.ts`는 서버가 메시지를 만든 시각, `live.data.at`은 그 값을 담은 tag55 push의 시각(`LiveRadar.at`)이다.

#### 14.6.3 슬롯, 조절, 느린 클라이언트 (G14, G15)

| 메시지 | 클라이언트 쪽 경로 | 넘칠 때 |
|--------|--------------------|---------|
| `seq`가 정수인 모든 메시지 | 순서 있는 큐(`maxsize=512`, M2 7.5절) | 그 연결만 1013으로 닫는다(G5, 그대로) |
| `live` | 슬롯 `"live:<device_id>"` (구독한 연결에만) | 덮어쓴다. 최신 값이 이긴다 |
| `countdown` | 슬롯 `"countdown"` (모든 연결) | 덮어쓴다 |

`Client`(ws.py)에 더하는 것:

```python
class Client:
    slots: dict[str, str]          # key -> latest serialized message; never counts toward the queue
    _wake: asyncio.Event           # set by put(), put_slot() and close()

    def put_slot(self, key: str, text: str) -> None:
        if self.close_code is not None:
            return
        self.slots.pop(key, None)  # re-insert at the end: sensors take turns
        self.slots[key] = text
        self._wake.set()

    def drop_slot(self, key: str) -> None:
        self.slots.pop(key, None)
```

- `put()`은 큐에 넣은 뒤 `_wake.set()`. `close()`는 큐와 함께 `slots`도 비운다.
- 보내기 루프(`run()`): 큐에 메시지가 있으면 그것 먼저(`None`이면 끝), 없으면 `slots`의 첫 항목을 꺼내 보내고, 둘 다 비었으면
  `_wake.clear()` 후 `await _wake.wait()`. 상태 메시지가 늘 실시간 값보다 먼저 나간다. 보내기 10초 제한과 1013은 M2와 같다.
- 조절은 **센서마다 서버 전체에서 한 번** 한다(`LIVE_INTERVAL_S = 0.25`). `LiveRadar`/`PirChanged`가 오면: 그 센서를 보는 연결이 없으면 무시.
  예약된 발행이 없으면 `delay = max(0, 마지막 발행 + 0.25 - 지금)`(루프 시계)으로 `loop.call_later(delay, emit, id)`를 하나 예약한다.
  예약이 이미 있으면 아무것도 하지 않는다. 첫 값은 바로 나가고(앞 가장자리), 0.25초 안에 온 값들은 마지막 것 하나로 합쳐 나간다(뒤 가장자리).
- `emit(id)`: 예약 표시를 지우고 "마지막 발행"을 지금으로. 세션이 없거나 `session.last_radar is None`이면 아무것도 보내지 않는다.
  아니면 그 순간의 코어 상태로 `LiveData`를 만든다(G3와 같은 원칙): `pir = session.last_pir`, `sub_sensor_presence = list(last_radar.sub_sensor_presence)`,
  존마다 `LiveZone(index, distance_m=dists[index], enabled, trigger_active, trigger=current_trigger, trigger_threshold, maintain=current_maintain, maintain_threshold)`.
  `dists = zone_distances(session.info) if session.info else FALLBACK_DISTANCES_M`(G23). 한 번 직렬화해 그 센서를 보는 모든 연결의 슬롯에 넣는다.
- `live_unsubscribe`를 받으면 그 연결의 `"live:<id>"` 슬롯도 지운다. 현재 라운드가 WAITING을 떠나면 모든 연결의 `"countdown"` 슬롯을 지운다.

#### 14.6.4 실시간 구독 = 세션 live 참조 (`ms605/gui/live.py`, G12)

```python
LIVE_INTERVAL_S = 0.25

class LiveFeed:
    def __init__(self, fleet: Fleet, hub: Hub) -> None: ...
    watchers: dict[str, set[Client]]                 # device_id -> connections that watch it
    def subscribe(self, client: Client, device_ids: Sequence[str]) -> None: ...
    def unsubscribe(self, client: Client, device_ids: Sequence[str]) -> None: ...
    def drop_client(self, client: Client) -> None: ...  # Hub.serve()의 finally
    def refresh(self, device_ids: Iterable[str]) -> None: ...  # 세션이 바뀌었을 수 있을 때
    def on_event(self, ev: Event) -> None: ...         # Hub.on_event가 넘긴다
    async def aclose(self) -> None: ...                # 예약·태스크 취소. 기기 I/O 없음
```

**맞추기(reconcile)**: 서버는 센서마다 "지금 참조를 잡고 있는 세션"(`held[id]`)을 하나 기억한다. 원하는 상태는
`want = fleet.sessions.get(id) if watchers.get(id) else None`이다. `held[id] is not want`이면:

1. `held`가 있으면 기억에서 지우고 `await held.release_live()`(오류는 코어가 로그만 남긴다).
2. `want`가 있으면 `held[id] = want`로 적고 `await want.acquire_live()`. `MS605Error`면 경고 로그만 남긴다. 코어 계약대로 카운트는
   올라간 채이고, 세션이 다시 CONNECTED가 되면 tag54=1을 스스로 다시 쓴다(코어 4.2절). 그래서 실패해도 "보고 있음" 상태는 맞다.

- 센서마다 맞추기 태스크는 한 번에 하나다. 돌고 있는 중에 다시 요청되면 표시만 하고, 태스크가 끝에서 표시를 보고 한 번 더 돈다.
  그래서 빠른 구독·해제가 번갈아 와도 tag54 쓰기가 겹치지 않고 마지막 상태로 수렴한다.
- 맞추기를 요청하는 곳: `subscribe`·`unsubscribe`·`drop_client`(바뀐 id들), `SensorGathered`와 `device_id`가 있는 `LinkStateChanged`
  (보는 사람이 있는 id만. `Fleet.connect()`가 주소가 바뀐 센서의 세션 객체를 바꾸는 경우), `POST /api/release`가 끝난 뒤의
  `refresh(ids)`(`release()`는 `close()`의 이벤트를 보낸 뒤에야 `sessions`에서 빼므로 이벤트만으로는 늦다).
- 보정 중(`busy == "calibration"`)에는 세션이 tag54를 건드리지 않고 기기가 스스로 tag55를 보낸다(코어 4.3절). 그래서 보정 중인 센서를
  보는 화면은 기기가 임계값을 조정하는 모습을 실시간으로 본다. 보정이 끝나면 세션이 카운트에 맞춰 tag54를 다시 맞춘다.
- 서버를 끝낼 때: `aclose()`는 기기 I/O 없이 예약과 태스크만 정리한다. 곧이어 `fleet.aclose()`가 모든 링크를 끊으므로 tag54=0을 따로 쓰지 않는다.

#### 14.6.5 배치 (`ms605/gui/batch.py`)

```python
COUNTDOWN_INTERVAL_S = 1.0
MAX_SCHEDULE_AHEAD_S = 86400.0

class BatchService:
    def __init__(self, fleet: Fleet, hub: Hub, *, speed: float = 1.0) -> None: ...
    # expected_s = EXPECTED_CALIBRATION_S / speed, timeout = CALIBRATION_TIMEOUT_S / speed
    current: BatchCalibration | None                  # 현재 라운드의 코어 배치
    def active(self) -> bool: ...                     # current가 WAITING 또는 RUNNING
    def members(self) -> frozenset[str]: ...          # active()이면 current.device_ids, 아니면 빈 집합
    def view(self) -> BatchView | None: ...
    def job_view(self, device_id: str) -> CalibrationJobView: ...
    def retryable_ids(self) -> list[str]: ...         # device_ids 중 job_view(id).retryable, 선택 순서
    def create(self, ids: Sequence[str], mode: StartMode, start: float | datetime, *, presence_override: bool) -> BatchView: ...
    def retry(self, ids: Sequence[str], mode: StartMode, start: float | datetime) -> BatchView: ...
    async def cancel(self) -> BatchView: ...
    def on_event(self, ev: Event) -> None: ...
    async def aclose(self) -> None: ...                                   # 카운트다운·모으기 정지 태스크 정리
```

14.5.3~14.5.5절의 **검사(404·409·422)는 모두 라우트(server.py)가 한다.** 라우트는 `active()`, `members()`, `view()`, `retryable_ids()`와
`fleet.sessions`로 검사하고 실패하면 기존 `ApiFailure`를 올린다. `BatchService`는 검사를 통과한 요청만 실행한다(`create`/`retry`는
`fleet.calibrate()`를 부르고 서버 배치를 갱신·카운트다운 시작·`batch` 표시, `cancel`은 현재 코어 배치의 `cancel()`). 이렇게 나누는 이유:
`batch.py`가 `server.py`를 import하면 순환이 된다(server → ws → batch).
같은 이유로 `live.py`와 `batch.py`는 `Hub`·`Client` 타입을 `if TYPE_CHECKING:` 안에서만 import한다(`ws.py`가 둘을 만든다: `Hub.__init__(..., speed=)`가 `self.live = LiveFeed(fleet, self)`, `self.batches = BatchService(fleet, self, speed=speed)`. `create_app()`은 `speed = sim.devices[0].speed if sim and sim.devices else 1.0`을 넘긴다).

서버 배치가 기억하는 것: `batch_id`(첫 라운드의 코어 id), `created_at`, `presence_override`, `device_ids`(선택 순서), `round`, 라운드의 `start` 방식,
센서별 "마지막 시도의 코어 배치"(`core_for[id]`), 센서별 `attempt`, 센서별 경과 시간(`elapsed[id]`, `CalibrationProgress`에서. 이벤트에서
옮겨 적는 유일한 값이다. 코어 객체가 경과 시간을 갖고 있지 않기 때문이다), 현재 재시도 라운드 대상의 직전 시도(`previous[id]` =
`(core_for, attempt, elapsed)`. 그 센서의 `CalibrationResult`가 `cancelled`·`started: false`로 오면 이벤트를 받은 자리에서 되돌린다. 14.5.5절).

**뷰는 언제나 코어 객체에서 다시 만든다**(G3):

`job_view(id)`: `core = core_for[id]`, `job = core.jobs.get(id)`,
`result = core.results.get(id) or (job.result if job else None)`. 코어의 `results`는 라운드가 끝날 때 채워지므로(발사 전 "세션 없음"과
WAITING 취소는 예외) 진행 중에는 `job.result`를 본다.

| 필드 | `result`가 있음 | `job`만 있음 | 둘 다 없음(발사 전) |
|------|-----------------|--------------|---------------------|
| `state` | `result.state` | `job.state` | `"idle"` |
| `started` | `result.started` | `state in ("starting", "learning")` | `False` |
| `error`, `detail` | 결과의 값 | `None`, `""` | `None`, `""` |
| `before`, `after` | `[ZonePair(trigger=t, maintain=m) for t, m in result.before]` (없으면 `None`) | `None` | `None` |
| `history_saved` | `result.history_saved` | `False` | `False` |

`elapsed_s = elapsed.get(id)`, `attempt = attempt[id]`, `retryable = state in ("failed", "lost", "timeout")`, `batch_id`는 서버 배치의 id.

`view()`: `BatchView(batch_id, state=current.state, round, start, fire_at=current.fire_at, created_at, expected_s, presence_override,
device_ids, round_ids=list(current.device_ids), jobs=[job_view(i) for i in device_ids])`. 배치가 없으면 `None`.

**카운트다운**: 라운드를 만들 때마다 태스크 하나: `while current.state is WAITING:` `Countdown(batch_id, fire_at=current.fire_at,
remaining_s=max(0, fire_at - now))`를 `CountdownMessage(ts=now)`로 직렬화해 모든 연결의 `"countdown"` 슬롯에 넣고 `COUNTDOWN_INTERVAL_S` 쉰다.
화면은 메시지 사이를 스스로 센다(14.8.2절). 서버 시계 기준 값만 보내므로 폰과 노트북의 시계가 달라도 같은 숫자를 본다.
예약(`at`)으로 몇 시간 기다리는 동안 링크는 세션 keep-alive가 유지한다(코어 6.2절). 노트북 잠자기 방지는 범위 밖이며 화면이 안내한다(14.8.10절).

#### 14.6.6 허브: 이벤트 → 메시지 (M3, 7.4절 표를 대신하는 행)

| 코어 이벤트 | 허브 처리 | WS |
|-------------|-----------|----|
| `LiveRadar`, `PirChanged` | `LiveFeed.on_event`: 보는 연결이 있는 id만 조절 후 발행(14.6.3절). `LiveRadar.at`을 기억 | `live` |
| `SensorGathered` | M2 처리 + `LiveFeed` 맞추기(보는 연결이 있으면) | M2와 같음 |
| `LinkStateChanged`, `device_id` 있음 | M2 처리 + `LiveFeed` 맞추기(보는 연결이 있으면) | M2와 같음 |
| `BatchChanged` | `batch_id == current.batch_id`이면 dirty(batch). RUNNING이 되면 카운트다운 정지·슬롯 지우기, 모으는 중이면 태스크로 `await fleet.stop_gather(finish_pending=True)` → `hub.clear_connecting()` → `mark_gather()` → `flush()`(G20). DONE/CANCELLED면 카운트다운 정지·슬롯 지우기 | `batch`, (`gather`) |
| `CalibrationStateChanged`, `CalibrationResult` | `device_id`가 `current.device_ids`에 있으면 dirty(job id) | `calibration_job` |
| `CalibrationProgress` | 위와 같고 `elapsed[id] = elapsed_s` | `calibration_job` |
| `ApplyResult` | 무시(M4) | — |

- `HANDLED_EVENTS`에 `LiveRadar`, `PirChanged`, `CalibrationStateChanged`, `CalibrationProgress`, `CalibrationResult`, `BatchChanged`를 옮기고
  `IGNORED_EVENTS`에는 `ApplyResult`만 남긴다. 덮개 테스트(M2 7.4절)는 그대로 통과해야 한다.
- 모으기 정지(G20)는 코어 이벤트(`BatchChanged(RUNNING)`)를 보고 하므로 작업들의 첫 I/O(보정 전 tag51 읽기)와 같은 틱 근처에서 일어난다.
  CLI처럼 "발사 직전"이 정확히 보장되지는 않는다. 코어에 발사 직전 훅을 더하지 않는 대가이며, 그 차이(스캔 1회 이내)는 미측정 영향 범위 안이다.
- `BusyChanged`는 M2대로 `sensor`를 다시 만든다. 그래서 보정 중인 센서는 어느 화면에서나 `작업 중 (보정 중)`으로 보인다(작업 잠금 표시).

#### 14.6.7 스냅샷과 늦게 붙은 화면

- `StateSnapshot.batch = batch_service.view()`. 진행 중이든 끝났든 서버가 가진 배치가 그대로 들어 있다. 그래서 중간에 붙은 폰도 같은 진행 막대,
  같은 결과, 같은 취소·재시도 버튼을 본다.
- `live`는 스냅샷에 없다. 클라이언트는 스냅샷을 받은 직후 다시 구독하고, 다음 push부터 받는다(실기기 약 1초).
- `countdown`도 스냅샷에 없다. 클라이언트는 `batch.fire_at - snapshot.ts`로 첫 값을 정하고(둘 다 서버 시계), 다음 `countdown`이 오면 그 값으로 맞춘다.

### 14.7 코어 접점

| GUI가 쓰는 것 | 어디서 | 비고 |
|---------------|--------|------|
| `Fleet.preflight(ids, window_s=)` | `POST /api/preflight` | 기기별 오류는 결과의 `error`(예외 아님) |
| `Fleet.calibrate(ids, start=float \| datetime, timeout=)` | 배치 생성·재시도 | 동기. `KeyError`/`ValueError`는 위 검사에서 대부분 먼저 걸리고, 남은 것(지난 시각)은 422 `invalid` |
| `BatchCalibration.batch_id`, `device_ids`, `fire_at`, `state`, `jobs`, `results`, `cancel()` | `BatchService` | `results`는 라운드 끝에 채워진다. 진행 중에는 `jobs[id].result` |
| `CalibrationJob.state`, `CalibrationJob.result` | `job_view()` | |
| `BatchChanged`, `CalibrationStateChanged`, `CalibrationProgress`, `CalibrationResult` | 허브 | 14.6.6절 |
| `DeviceSession.acquire_live()` / `release_live()` | `LiveFeed` | 서버 전체에서 기기당 하나(G12). 사전점검의 참조와 같은 카운트를 쓴다 |
| `DeviceSession.last_radar`, `last_pir`, `info`, `state`, `busy` | `LiveFeed.emit`, 라우트 검사 | |
| `LiveRadar`, `PirChanged` | `LiveFeed` | |
| `models.zone_distances(session.info)`, `FALLBACK_DISTANCES_M` | `LiveFeed.emit` | 코어 2.2절(`DeviceInfo.zone_distances_m`) |
| `EXPECTED_CALIBRATION_S`, `CALIBRATION_TIMEOUT_S` | `BatchService` | 시뮬레이터 속도로 나눈다 |
| `CalibrationJob(identify_wait=IDENTIFY_WAIT_S)` | 코어 `BatchCalibration`이 기본값으로 만든다 | **M3 항목 3.** GUI는 아무것도 하지 않는다. 배치 생성 검사가 `"identify"`를 잠금으로 보지 않고(14.5.3절 4단계), 발사된 작업이 최대 2초 기다린다(코어 5.2절). 2초를 넘기면 지금처럼 `FAILED("busy: identify")` |

GUI 배치의 "한 번에 하나"는 서버 규칙이다. 코어는 배치 여러 개를 허용한다(코어 6.2절).

lifespan(8.4절)의 종료 순서: `hub.close_clients()` → `await hub.live.aclose()` → `await hub.batches.aclose()` → `await fleet.aclose()`(끝나지 않은 배치를 취소하고
모든 링크를 해제한다. 보정 중이던 센서는 코어 5.3절대로 링크가 끊긴다) → `hub.detach()`.

### 14.8 프런트엔드

M2의 시각 언어(토큰, `StatusBadge`의 아이콘 + 문구 + 색, 화면마다 주 동작 하나, 폰 하단 고정 주 버튼, 접힌 보조 영역)를 그대로 쓴다.
새 색 토큰은 만들지 않는다.

#### 14.8.1 라우트와 내비게이션

| 경로 | 화면 |
|------|------|
| `/monitor` | 실시간 모니터. `?ids=a,b`(쉼표로 구분한 device id)가 있으면 그 센서들을 처음 선택으로 쓴다 |
| `/calibrate` | 다중 자동 보정. `?ids=a,b`가 있으면 선택 단계의 처음 체크로 쓴다 |
| `/sensors/:deviceId` | M2 + `LiveStrip`(세션이 있을 때) + `이 센서 보정` 링크(`/calibrate?ids=<id>`, CONNECTED일 때) + `detail.comingSoon`(설정 편집만) |

- `AppShell`의 탭은 4개: 대시보드(`LayoutGrid`), 센서 모으기(`Bluetooth`), 모니터(`Activity`), 보정(`Crosshair`). 폰 하단 탭 바도 4칸(칸마다 90px 안팎, 문구 12px).
- 헤더에 `CalibrationPill`: 현재 라운드가 WAITING이면 `[Timer] 보정 0:42 후`(예약이면 `보정 14:30`), RUNNING이면 `[Loader2 회전] 보정 중 1/3`.
  그 밖에는 숨긴다. 누르면 `/calibrate`. 모든 화면에서 카운트다운과 진행이 보인다(D11).

#### 14.8.2 스토어와 리듀서

```ts
interface CountdownAnchor {
  batch_id: string
  remaining_s: number   // 서버가 잰 남은 시간
  received_at: number   // 받은 시각 (Date.now(), ms)
}
interface AppState {
  // ... M2 그대로
  batch: BatchView | null
  countdown: CountdownAnchor | null
  live: Record<string, LiveData>      // device_id -> 최신 프레임
  watch: Record<string, number>       // 이 화면들의 구독 참조 수. 서버에서 오지 않는다
}
export function reduce(state: AppState, msg: IncomingMessage, now: number = Date.now()): { state: AppState; resync: boolean }
export function addWatch(state: AppState, ids: readonly string[]): AppState     // 수 + 1
export function removeWatch(state: AppState, ids: readonly string[]): AppState  // 수 - 1, 0이면 키와 live[id]를 지운다
```

`now`는 순수 함수를 지키려고 인자로 받는다(테스트가 넘긴다). 순서 검사(M2 7.6절)는 그대로이고, 각 메시지의 적용 규칙:

| 메시지 | 적용 |
|--------|------|
| `snapshot` | M2 + `batch = data.batch`, `live = {}`, `countdown = batch?.state === 'waiting' ? {batch_id, remaining_s: max(0, fire_at - msg.ts), received_at: now} : null`. `watch`는 그대로 |
| `batch` | `batch = data`. `data.state === 'waiting'`이면 `countdown`을 위와 같이 다시 정하고, 아니면 `null` |
| `calibration_job` | `batch?.batch_id === data.batch_id`이면 `jobs`에서 같은 `device_id` 항목을 바꾼다(순서 유지). 아니면 그대로(`lastSeq`는 오른다) |
| `live` (`seq: null`) | `(watch[data.device_id] ?? 0) > 0`이면 `live[data.device_id] = data`, 아니면 무시 |
| `countdown` (`seq: null`) | `batch?.batch_id === data.batch_id`이고 `batch.state === 'waiting'`이면 `countdown = {batch_id, remaining_s: data.remaining_s, received_at: now}`. 아니면 무시(RUNNING 뒤에 늦게 온 값을 버린다) |

- 표시할 남은 시간 = `max(0, remaining_s - (Date.now() - received_at) / 1000)`. 1초마다 오는 `countdown`이 다시 맞추므로 폰이 잠들었다 깨도 금방 맞는다.
- 스토어 액션: `watch(ids)`, `unwatch(ids)`(위 두 함수를 감쌈). 셀렉터: `selectBatchActive`(라운드가 waiting/running), `selectBatchMembers`(활성이면
  `round_ids`의 `Set`), `selectBatchTally(batch)` → `{ ended, total, succeeded, failed }`(`round_ids` 기준. `failed`는 failed·lost·timeout).

#### 14.8.3 WS와 구독 (`api/ws.ts`, `hooks/useLiveWatch.ts`)

- `SocketLike`에 `send(data: string): void`를 더한다.
- 연결마다 "서버에 구독을 보낸 id 집합"(`sent`)을 둔다. `syncWatch()`: 소켓이 열려 있고 그 소켓에서 스냅샷을 받았을 때만,
  `want = Object.keys(watch)`와 `sent`의 차이로 `live_subscribe`(더할 것)와 `live_unsubscribe`(뺄 것)를 32개씩 끊어 보내고 `sent`를 갱신한다.
- `syncWatch()`를 부르는 곳: 스냅샷을 받은 직후(`sent`를 비운 뒤. 서버가 새 연결의 구독을 모르기 때문이다), 그리고 `useStore.subscribe`로 본
  `watch` 객체가 바뀔 때. 스냅샷 전에는 아무것도 보내지 않는다.
- `useLiveWatch(ids: readonly string[])`: `ids`를 정렬해 이은 문자열을 의존성으로 `useEffect(() => { watch(ids); return () => unwatch(ids) })`.
  모니터 화면과 센서 상세의 실시간 띠가 쓴다. 같은 센서를 두 컴포넌트가 봐도 서버 구독은 하나다(참조 수).
- `hooks/useCountdown.ts`: `countdown`이 있으면 250 ms마다 다시 그리며 남은 초(실수)를 돌려준다. 없으면 `null`.

#### 14.8.4 막대 (`meter.ts`) — CLI `_ui.meter`의 그대로 옮긴 판

CLI 실시간 모니터(`ms605/cli/cli.py`의 `_MONITOR_BAR_WIDTH = 16`, `_MONITOR_TICK = 5`)와 **같은 칸 수, 같은 계산**이다. 이 코드를 그대로 쓴다:

```ts
export const METER_WIDTH = 16 // ms605/cli/cli.py _MONITOR_BAR_WIDTH
export const METER_TICK = 5 // ms605/cli/cli.py _MONITOR_TICK: the threshold column, the same for every bar

export type MeterCell = 'fill' | 'tick' | 'empty'

export interface MeterModel {
  cells: MeterCell[] // length = width; cells[tickAt] is always 'tick'
  over: boolean // value > threshold: the fill crosses the tick and is drawn red
}

/** Port of ms605.cli._ui.meter: the threshold tick sits at a fixed column; the fill is drawn
 *  from the signed offset value - threshold, one cell = max(|threshold| / tickAt, 1) units. */
export function meter(value: number, threshold: number, width = METER_WIDTH, tickAt = METER_TICK): MeterModel {
  if (!Number.isInteger(width) || !Number.isInteger(tickAt) || tickAt < 1 || tickAt > width - 2) {
    throw new RangeError(`tickAt must be in [1, ${width - 2}] for width=${width}, got ${tickAt}`)
  }
  const step = Math.max(Math.abs(threshold) / tickAt, 1)
  const offset = value - threshold
  const filled =
    offset > 0
      ? tickAt + 1 + Math.min(width - tickAt - 1, Math.ceil(offset / step))
      : Math.max(0, tickAt - Math.ceil(-offset / step))
  const cells = Array.from({ length: width }, (_, i): MeterCell => (i === tickAt ? 'tick' : i < filled ? 'fill' : 'empty'))
  return { cells, over: offset > 0 }
}

/** The CLI's characters, for tests: fill '█', tick '┃', empty '─'. */
export function meterPlain(m: MeterModel): string {
  return m.cells.map((c) => (c === 'fill' ? '█' : c === 'tick' ? '┃' : '─')).join('')
}
```

의미(CLI docstring과 같다):

- tick은 임계값과 관계없이 늘 `tickAt` 칸에 있다. 임계값이 프레임마다 바뀌어도 tick이 움직이지 않는다.
- `value < threshold` → 채움이 tick 앞에서 멈춘다(빈칸 1개 이상). `value == threshold` → tick 바로 앞까지 꽉 찬다. `value > threshold` → tick을 넘어 1칸 이상, **빨강**.
- 그래서 "막대가 tick을 넘었는가" = "값이 임계값보다 큰가"다. 보정이 만든 0이나 음수 임계값에서도 같다.
- 넘친 값은 양끝에서 잘린다(`999/10`은 꽉 참, `-999/10`은 빈 막대).

**기준 사례**(`web/src/test/meter_cases.json`, 아래 표를 `{"value","threshold","width","tick_at","plain","over"}` 객체 배열로 그대로 옮긴다).
이 표는 이 문서를 쓸 때 `ms605.cli._ui.meter`로 실제로 만든 값이다. **파이썬 테스트와 TS 테스트가 같은 파일을 읽어** 두 구현이 같은지 확인한다.

| value | threshold | width | tick_at | plain | over |
|------:|----------:|------:|--------:|-------|------|
| 60 | 60 | 16 | 5 | `█████┃──────────` | false |
| 61 | 60 | 16 | 5 | `█████┃█─────────` | true |
| 59 | 60 | 16 | 5 | `████─┃──────────` | false |
| 55 | 60 | 16 | 5 | `████─┃──────────` | false |
| 70 | 60 | 16 | 5 | `█████┃█─────────` | true |
| 77 | 60 | 16 | 5 | `█████┃██────────` | true |
| 120 | 60 | 16 | 5 | `█████┃█████─────` | true |
| 0 | 60 | 16 | 5 | `─────┃──────────` | false |
| 999 | 10 | 16 | 5 | `█████┃██████████` | true |
| -999 | 10 | 16 | 5 | `─────┃──────────` | false |
| 0 | 0 | 16 | 5 | `█████┃──────────` | false |
| 5 | 0 | 16 | 5 | `█████┃█████─────` | true |
| -1 | 0 | 16 | 5 | `████─┃──────────` | false |
| 1 | 1 | 16 | 5 | `█████┃──────────` | false |
| 2 | 1 | 16 | 5 | `█████┃█─────────` | true |
| 0 | 1 | 16 | 5 | `████─┃──────────` | false |
| -33 | -33 | 16 | 5 | `█████┃──────────` | false |
| -34 | -33 | 16 | 5 | `████─┃──────────` | false |
| -10 | -33 | 16 | 5 | `█████┃████──────` | true |
| 0 | -33 | 16 | 5 | `█████┃█████─────` | true |
| 10 | 10 | 20 | 6 | `██████┃─────────────` | false |
| 200 | 200 | 20 | 6 | `██████┃─────────────` | false |
| 0 | 200 | 20 | 6 | `──────┃─────────────` | false |
| 101 | 100 | 24 | 8 | `████████┃█──────────────` | true |
| 40 | 100 | 24 | 8 | `███─────┃───────────────` | false |

`over`는 CLI 쪽에서 "채움 칸 중 하나라도 `bar.active` 스타일인가"다(`tests/test_ui.py`의 `_is_red`와 같다).

`tests/test_ui.py`의 막대 테스트도 같은 경우로 TS에 옮긴다(14.9.2절): tick 고정 열(`thr` 10·55·200, width 20, tick 6), 임계값 60·1·0·-33에서
"같으면 tick까지, +1이면 넘음, -1이면 못 미침", 가까운 값 구별(55·59·60·70·77 vs 60), 음수 임계값(-10·0 vs -33이 빨강), 0 임계값에서 작은 값이
꽉 차지 않음(`5/0`: 넘은 칸 1~9), 색은 값 대 임계값을 따름, 넘친 값 자르기, `tickAt` 범위 밖(`(1,0)`, `(10,0)`, `(10,9)`, `(10,10)`)은 `RangeError`.

**`Meter` 컴포넌트** (`components/Meter.tsx`):

```tsx
<div className={styles.meter} data-over={over}>
  <span className={styles.meterLabel}>{label}</span>
  <div className={styles.track} role="img" aria-label={t.live.meterAria(label, value, threshold, over)}>
    {cells.map((c, i) => <span key={i} className={styles.cell} data-cell={c} />)}
  </div>
  <span className={styles.value} data-hot={hot}>{value}/{threshold}</span>
</div>
```

- props: `value`, `threshold`, `label`(`재실 트리거`/`재실 유지`), `hot`, `compact?`(라벨 숨김, 높이 8px). `cells`/`over`는 `meter(value, threshold)`.
- 트랙: `display: grid; grid-template-columns: repeat(16, 1fr); gap: 2px; height: 12px`. 칸 `border-radius: 2px`.
  `fill` = `var(--ok)`, `[data-over=true]`의 `fill` = `var(--danger)`, `empty` = `var(--surface-2)` + `1px solid var(--border)`.
  `tick` 칸은 배경 없이 가운데에 폭 3px `var(--text)` 세로선을 트랙 위아래로 3px씩 넘치게 그린다. 전환 애니메이션은 없다(4 Hz로 바뀐다).
- 값 글자: `font-variant-numeric: tabular-nums`, 최소 7ch. `hot`이면 `var(--danger)` 굵게. 트리거의 `hot`은 기기의 `trigger_active` 플래그, 유지의 `hot`은
  `maintain > maintain_threshold`다(CLI `render_monitor`와 같다. 트리거 플래그는 막대 비교와 다를 수 있다).
- 색만으로 전달하지 않는다: tick을 넘은 모양, 숫자, `aria-label`의 `초과`/`이하`가 같은 정보를 준다.

#### 14.8.5 실시간 컴포넌트

- `ZoneMeterRow`: `LiveZone` 하나. 왼쪽 라벨 `Z0 · 0.8 m`, 그 옆에 트리거 `Meter`와 유지 `Meter`. `enabled === false`면 막대 대신 `꺼짐`(흐린 글자).
  `compact`면 트리거 막대만.
- `PresenceChips`: `PIR 감지`(`Eye`, `--danger-bg` 위 `--danger`) / `PIR 없음`(`EyeOff`, off) / `PIR ?`(값을 아직 못 봄). 서브센서 `S1 재실`(`UserRound`, ok) / `S1 부재`(off).
  CLI 헤더 줄과 같은 정보다.
- `SensorLiveCard`: 별명(없으면 BLE 이름) + `StatusBadge` + `PresenceChips` + 존 7줄. 상태별 본문:
  - `live.link !== 'connected'`: 카드 전체 투명도 0.6, `StatusBadge`의 hint(예: "센서 버튼을 다시 누르세요"), 받은 값이 있으면 그대로 두고 `실시간 값 없음 (마지막 값)`.
  - 연결됐지만 프레임 없음: `데이터 수신 대기 중…`.
  - `busy === 'calibration'`: 위에 `보정 중 — 기기가 임계값을 조정하고 있습니다`(막대는 계속 움직인다).
- `SensorPicker`: 세션이 있는 센서(`live !== null`)를 사이트·별명 순(미등록은 맨 뒤)으로 토글 칩(`aria-pressed`). `모두 선택`/`모두 해제` 보조 버튼. 줄바꿈(가로 스크롤 없음).
- `MonitorScreen`(`h1` 실시간 모니터): 선택 초깃값은 `?ids=` → `localStorage['ms605.monitorIds']`(지금 세션이 있는 것만, try/catch) → CONNECTED 센서 전부.
  선택이 바뀌면 저장한다. `useLiveWatch(선택)`. 범례(`monitor.legend`, `monitor.legendTrigger`)는 화면에 한 번. 세션이 하나도 없으면 `EmptyState`(`monitor.empty` + `센서 모으기`).
  주 동작은 없다(보기 화면).
- `LiveStrip`(센서 상세): `useLiveWatch([id])`, 제목 `실시간`, `PresenceChips`, `compact` 존 7줄, 링크 `모니터에서 크게 보기`(`/monitor?ids=<id>`).

#### 14.8.6 보정 화면의 흐름

`CalibrateScreen`(`h1` 다중 자동 보정)은 서버 배치와 화면 상태로 보일 단계를 고른다(위에서 처음 맞는 것):

| 조건 | 보이는 것 |
|------|-----------|
| `batch`가 있고 `state`가 `waiting`/`running` | `BatchProgress` — 모든 화면이 같다(시작한 화면이 아니어도) |
| `batch`가 있고 `done`/`cancelled`이며 이 화면이 그 `batch_id`를 닫지 않았음 | `BatchResults` (+ `RetryPanel`) |
| 그 밖 | 화면 상태 `step`: `select` → `check`(사전점검 + 시작 방법) |

- "닫음"은 `새 보정` 버튼이 `sessionStorage['ms605.dismissedBatch'] = batch_id`(try/catch)로 남긴다. 다른 화면에는 영향이 없다(결과는 서버에 계속 있다).
- `select`/`check`의 선택·시작 방법은 화면마다의 상태다(D11의 초안과 같은 취급). 다른 화면이 배치를 시작하면 이 화면도 곧바로 `BatchProgress`로 바뀐다.

**`SelectStep`**: 세션이 있는 센서 목록(사이트·별명 순). 줄마다 체크박스, 이름, `StatusBadge`. CONNECTED가 아니거나 `busy`가 `identify` 밖의 값이면 체크할 수 없고
이유를 보인다(`calib.notConnected` 또는 `작업 중 (…)`). `연결된 센서 모두 선택` 보조 버튼. 주 버튼(폰 하단 고정) `다음: 사전점검 (N대)`, N=0이면 비활성.

**`PreflightStep`**(`check`): 들어오면 `POST /api/preflight {device_ids}`를 부르고 그동안 `사람이 있는지 확인하는 중… (약 3초)`. 결과 줄은 14.8.9절 표.
`다시 확인` 보조 버튼. 그 아래 `StartOptions`와 주 버튼. 주 버튼 문구는 방식별(`보정 시작` / `30초 후 보정 시작` / `14:30에 보정 예약`).
- `occupied === true`인 센서가 있고 방식이 `지금`이면 체크박스 `사람이 없는 것을 확인했습니다 (경고 무시하고 시작)`를 켜야 주 버튼이 켜진다.
  켜고 시작하면 `presence_override: true`(G19).
- 방식이 `N초 후`/`시각 예약`이면 체크박스 대신 `시작 전에 방을 비워 주세요…` 안내만 보인다.
- 사전점검 요청이 실패하면(409 `busy` 등) `errorText(code)`를 보이고 `다시 확인`으로 다시 시도한다. 사전점검은 경고일 뿐이므로 실패해도 시작은 막지 않는다.

**`StartOptions`**: 라디오 그룹(분할 버튼 모양) `지금` / `N초 후` / `시각 예약`.
- `N초 후`: 숫자 입력(1~3600, 기본 30) + 빠른 선택 `10`·`30`·`60`·`120`초.
- `시각 예약`: `<input type="time">`(기본: 지금부터 10분 뒤를 5분 단위로 올림). `resolveAt(hhmm, now)`는 오늘 그 시각이 지금보다 뒤면 오늘, 아니면 내일이다
  (CLI `resolve_target_datetime`과 같은 규칙). 아래에 `오늘 14:30` / `내일 07:00`과 `start.atNote`를 보인다. 보낼 때 `at = date.toISOString()`(UTC `Z`도 시간대다).
- `props.modes`로 보일 방식을 고른다. 재시도 화면은 `['now', 'delay']`만 쓴다.

**`BatchProgress`**:
- 머리말(14.8.9절 배치 표). WAITING: 큰 글씨 카운트다운 `0:42`(`useCountdown`, 1시간 넘으면 `h:mm:ss`). 예약이면 `14:30에 보정 시작 · 2시간 3분 남음`.
- 취소: WAITING은 테두리 위험 버튼 `카운트다운 취소`(예약이면 `예약 취소`), 확인 없이 바로 `POST …/cancel`(빨리 멈출 수 있어야 한다).
  RUNNING은 `보정 취소` + `ConfirmDialog`(`batch.cancelConfirm`): 링크를 끊으므로 되돌릴 수 없는 동작이다(10.1절 5).
- `JobRow` 목록(`device_ids` 순). RUNNING 동안 `batch.progressNote`를 작은 보조 글씨로 한 번(G21). 모으기가 꺼져 있으면 `batch.gatherPaused`.
  `presence_override`면 `batch.override`를 작은 경고 칩으로.
- 이번 라운드에 들지 않은 센서(재시도 라운드에서 이미 끝난 센서)는 결과 그대로 흐리게 보인다.

**`JobRow`**: 이름, `jobStatus()`의 아이콘·문구·hint, `attempt > 1`이면 `2번째 시도` 칩. LEARNING이면 진행 막대(`role="progressbar"`, `aria-valuemin=0`,
`aria-valuemax=100`, `aria-valuenow`=비율 × 100 반올림, `aria-valuetext`=`batch.progressAria`)와 `1:12 / 약 3:00`. 예상 시간을 넘기면 막대를 99%에 두고
`batch.overdue`를 보인다. STARTING과 발사 직후의 IDLE은 막대 대신 회전 아이콘. `prefers-reduced-motion`이면 회전을 끈다.

**`BatchResults`**: 머리말(`보정 끝 · 성공 2 · 실패 1` 또는 `보정을 취소했습니다`), `JobRow` 목록, 성공한 센서마다 접힌 `전후 비교 보기` → `ThresholdCompare`,
`RetryPanel`(재시도할 센서가 있을 때), 보조 버튼 `새 보정`. 성공 + `history_saved`이면 줄에 `보정 기록에 저장했습니다`.

**`ThresholdCompare`**: `before`/`after`가 둘 다 있을 때만. 표(caption `<별명> 임계값 전후`): 열 `존` · `재실 트리거` · `재실 유지`, 칸은 `70 → 64 (−6)`(증가는 `+`, 같으면 `(0)`).
폰에서는 존마다 두 줄로 쌓는다. 둘 중 하나라도 없으면 `compare.none`.

**`RetryPanel`**: 후보 = `retryable` 작업. 줄마다:
- 그 센서가 지금 CONNECTED → 체크박스(기본 체크) + `다시 연결됨`.
- 아니면 체크할 수 없고 hint: 모으는 중이면 `버튼을 다시 누르세요`, 아니면 `센서 모으기를 켜고 버튼을 다시 누르세요` + 보조 버튼 `센서 모으기 시작`(`startGather()`).
  G20 때문에 발사 뒤에는 모으기가 꺼져 있으므로, 끊긴 센서를 되살리는 길이 이 버튼이다.
- `StartOptions modes=['now','delay']`, 기본 `N초 후` 30초: 끊긴 센서의 버튼을 누르러 방에 들어간 사람이 나갈 시간이다. 사전점검은 다시 하지 않는다.
- 주 버튼 `다시 시도 (N대)` → `POST /api/batches/{id}/retry {device_ids: 체크한 것, start, delay_s}`. 서버 배치가 WAITING/RUNNING이 되면 화면은 저절로 `BatchProgress`가 된다.

**작업 잠금 표시**(G22): 보정 중인 센서는 어디서나 `작업 중 (보정 중)`(M2 `status.ts`). 진행 중인 라운드의 센서는 센서 상세의 `연결 해제`와 대시보드의
`모두 연결 해제`가 비활성이고 `release.blockedByBatch`를 보인다. 그래도 409 `batch_active`가 오면(다른 화면이 방금 시작) `errorText`로 보인다.

#### 14.8.7 `api/client.ts`에 더할 함수

```ts
export const preflight = (deviceIds: string[], windowS?: number) =>
  request<PreflightResult>('POST', '/api/preflight', windowS === undefined ? { device_ids: deviceIds } : { device_ids: deviceIds, window_s: windowS })
export const createBatch = (body: { device_ids: string[]; start: StartMode; delay_s?: number; at?: string; presence_override?: boolean }) =>
  request<BatchView>('POST', '/api/batches', body)
export const getBatch = (batchId: string) => request<BatchView>('GET', `/api/batches/${enc(batchId)}`)
export const cancelBatch = (batchId: string) => request<BatchView>('POST', `/api/batches/${enc(batchId)}/cancel`)
export const retryBatch = (batchId: string, body: { device_ids?: string[]; start: StartMode; delay_s?: number; at?: string }) =>
  request<BatchView>('POST', `/api/batches/${enc(batchId)}/retry`, body)
```

보내지 않는 필드는 서버 기본값이 된다(`In` 모델의 기본값). 응답은 폼의 성공·실패 표시에만 쓰고, 화면 상태는 WS의 `batch`로 바뀐다(G10). 사전점검 결과만은 응답을 그대로 그린다(G18).

#### 14.8.8 `calibration.ts` (순수 함수, 테스트 대상)

```ts
export function jobStatus(job: CalibrationJobView, ctx: { batchState: BatchState; gathering: boolean; link: LinkState | null }): Status
export function presenceStatus(p: PresenceView): Status
export function jobProgress(job: CalibrationJobView, expectedS: number): { ratio: number | null; overdue: boolean }
export function batchHeadline(batch: BatchView, remainingS: number | null, now: Date): string
export function formatCountdown(s: number): string   // 올림. 59.2 → "1:00", 3600 → "1:00:00"
export function formatElapsed(s: number): string     // 내림. 72.9 → "1:12"
export function resolveAt(hhmm: string, now: Date): Date
```

- `Status`는 M2 `status.ts`의 `{ kind, icon, label, hint? }`이고 `StatusBadge`가 그대로 그린다.
- `jobProgress`: `learning`이면 `elapsed_s ?? 0`으로 `ratio = min(elapsed / expectedS, 0.99)`, `overdue = elapsed > expectedS`. `succeeded`면 `ratio = 1`.
  그 밖(`idle`, `starting`, 다른 종료 상태)은 `ratio = null`(막대 없음).

#### 14.8.9 상태 문구 (microcopy)

**작업 상태** — `jobStatus()`, 위에서부터 처음 맞는 행. LOST의 hint는 `ctx`로 고른다(아래 표 끝).

| 조건 | kind | 아이콘 | label | hint |
|------|------|--------|-------|------|
| `idle`, 배치 `waiting` | `off` | `Clock` | 시작 대기 | — |
| `idle`, 배치 `running` | `progress` | `Loader2`(회전) | 시작 준비 중… | — |
| `starting` | `progress` | `Loader2`(회전) | 시작하는 중… | — |
| `learning` | `progress` | `Hourglass` | 학습 중 | 방을 비워 두세요 |
| `succeeded`, `detail`이 `반영값 재조회 실패`로 시작 | `warn` | `CheckCircle2` | 완료 (결과값을 읽지 못함) | 전후 비교를 할 수 없습니다 |
| `succeeded`, `detail`이 `결과 저장 실패`로 시작 | `warn` | `CheckCircle2` | 완료 (기록 저장 실패) | 보정은 센서에 반영되었습니다 |
| `succeeded` | `ok` | `CheckCircle2` | 완료 | `history_saved`면 보정 기록에 저장했습니다 |
| `failed`, `error`가 `busy: `로 시작 | `warn` | `AlertTriangle` | 시작하지 못함 (다른 작업 중) | 잠시 뒤 다시 시도하세요 |
| `failed`, `error === null` | `danger` | `XCircle` | 실패 | 센서가 보정 실패를 알렸습니다. 방을 비우고 다시 시도하세요 |
| `failed` | `danger` | `XCircle` | 실패 | 오류: {error} |
| `lost`, `started` | `warn` | `AlertTriangle` | 연결 끊김 (학습 초기화됨) | (LOST hint) |
| `lost` | `warn` | `AlertTriangle` | 연결 끊김 (시작 전) | (LOST hint) |
| `timeout` | `danger` | `Clock` | 응답 없음 | 센서가 완료를 알리지 않았습니다. 다시 시도하세요 |
| `cancelled`, `started` | `off` | `CircleSlash` | 취소됨 | 연결을 끊어 학습을 멈췄습니다. 다시 연결하려면 버튼을 누르세요 |
| `cancelled` | `off` | `CircleSlash` | 취소됨 | — |

LOST hint: 배치가 `running`이면 `이번 보정이 끝난 뒤 다시 시도할 수 있습니다` → 아니면 `link === 'connected'`면 `다시 연결됨 · 다시 시도할 수 있습니다`
→ 모으는 중이면 `버튼을 다시 누르세요` → 아니면 `센서 모으기를 켜고 버튼을 다시 누르세요`.

**배치 머리말** — `batchHeadline()`:

| 조건 | 문구 |
|------|------|
| `waiting`, `start === 'now'` | 시작하는 중… |
| `waiting`, `delay` | {m:ss} 후 보정 시작 |
| `waiting`, `at` | {HH:MM}에 보정 시작 · {남은 시간} 남음 (`2시간 3분`, `12분`, 1분 미만은 `1분 미만`) |
| `running` | 보정 중 · {끝난 수}/{라운드 수} 끝남 |
| `done` | 보정 끝 · 성공 {s} · 실패 {f} (`f`는 failed·lost·timeout, 배치 전체 기준) |
| `cancelled` | 보정을 취소했습니다 |

**사전점검** — `presenceStatus()`:

| 조건 | kind | 아이콘 | label | hint |
|------|------|--------|-------|------|
| `occupied === true` | `warn` | `UserRound` | 아직 사람 있음 | 근거: {센서 재실 / PIR 감지 / 센서 재실·PIR 감지} |
| `occupied === false` | `ok` | `CheckCircle2` | 비어 있음 | — |
| `error !== null` | `off` | `HelpCircle` | 확인하지 못함 | {error} |
| 그 밖(`samples === 0`) | `off` | `HelpCircle` | 확인하지 못함 | 값을 받지 못했습니다 |

#### 14.8.10 문구 (`strings.ts`에 더할 키)

| 키 | 문구 |
|----|------|
| `nav.monitor` / `nav.calibrate` | 모니터 / 보정 |
| `monitor.title` / `monitor.pick` | 실시간 모니터 / 볼 센서 |
| `monitor.all` / `monitor.none` | 모두 선택 / 모두 해제 |
| `monitor.empty` / `monitor.emptyHint` | 연결된 센서가 없습니다 / 센서를 모으면 여기에서 실시간 값을 볼 수 있습니다 |
| `monitor.noneSelected` | 볼 센서를 고르세요 |
| `monitor.legend` | ┃ = 임계값(고정) · 막대가 ┃를 넘으면 빨강 = 임계값 초과 |
| `monitor.legendTrigger` | 트리거 숫자가 빨간색이면 센서가 재실을 감지한 것입니다 |
| `live.trigger` / `live.maintain` | 재실 트리거 / 재실 유지 |
| `live.zone` | Z{i} · {m} m |
| `live.zoneOff` | 꺼짐 |
| `live.waiting` / `live.stale` | 데이터 수신 대기 중… / 실시간 값 없음 (마지막 값) |
| `live.calibrating` | 보정 중 — 기기가 임계값을 조정하고 있습니다 |
| `live.pirOn` / `live.pirOff` / `live.pirUnknown` | PIR 감지 / PIR 없음 / PIR ? |
| `live.subOn` / `live.subOff` | S{n} 재실 / S{n} 부재 |
| `live.meterAria` | {label} {value}, 임계값 {threshold}, 초과 (넘지 않으면 `이하`) |
| `live.stripTitle` / `live.openMonitor` | 실시간 / 모니터에서 크게 보기 |
| `calib.title` / `calib.thisSensor` | 다중 자동 보정 / 이 센서 보정 |
| `calib.selectTitle` / `calib.selectHint` | 보정할 센서를 고르세요 / 연결된 센서만 고를 수 있습니다 |
| `calib.selectAll` / `calib.next` | 연결된 센서 모두 선택 / 다음: 사전점검 ({n}대) |
| `calib.notConnected` / `calib.back` | 연결되어 있지 않음 / 선택 바꾸기 |
| `preflight.title` / `preflight.running` | 사전점검 / 사람이 있는지 확인하는 중… (약 {s}초) |
| `preflight.empty` / `preflight.occupied` / `preflight.unknown` | 비어 있음 / 아직 사람 있음 / 확인하지 못함 |
| `preflight.because` / `preflight.bySub` / `preflight.byPir` | 근거: {reasons} / 센서 재실 / PIR 감지 |
| `preflight.noSamples` / `preflight.recheck` | 값을 받지 못했습니다 / 다시 확인 |
| `preflight.override` | 사람이 없는 것을 확인했습니다 (경고 무시하고 시작) |
| `preflight.leaveRoom` | 시작 전에 방을 비워 주세요. 카운트다운이 끝나면 보정이 시작됩니다 |
| `start.title` / `start.now` / `start.delay` / `start.at` | 시작 방법 / 지금 / N초 후 / 시각 예약 |
| `start.seconds` / `start.atToday` / `start.atTomorrow` | 초 / 오늘 {hhmm} / 내일 {hhmm} |
| `start.goNow` / `start.goDelay` / `start.goAt` | 보정 시작 / {n}초 후 보정 시작 / {label}에 보정 예약 |
| `start.atNote` | 예약 시각까지 센서 연결을 유지합니다(배터리를 씁니다). 이 컴퓨터가 잠자기에 들어가지 않게 하세요. |
| `batch.startingNow` / `batch.countdown` / `batch.scheduled` | 시작하는 중… / {mmss} 후 보정 시작 / {hhmm}에 보정 시작 · {rel} 남음 |
| `batch.running` / `batch.done` / `batch.cancelled` | 보정 중 · {done}/{total} 끝남 / 보정 끝 · 성공 {s} · 실패 {f} / 보정을 취소했습니다 |
| `batch.remainH` / `batch.remainM` / `batch.remainLt1` | {h}시간 {m}분 / {m}분 / 1분 미만 |
| `batch.cancelWaiting` / `batch.cancelScheduled` / `batch.cancelRunning` | 카운트다운 취소 / 예약 취소 / 보정 취소 |
| `batch.cancelConfirm` | 보정을 취소하면 센서 연결을 끊어 학습을 멈춥니다. 다시 연결하려면 각 센서의 버튼을 눌러야 합니다. |
| `batch.progressNote` | 막대는 예상 시간(약 {mmss}) 기준입니다. 센서는 진행률을 알려 주지 않습니다. |
| `batch.progressAria` | 약 {pct}% (예상 시간 기준) |
| `batch.elapsed` / `batch.overdue` | {elapsed} / 약 {expected} / 예상보다 오래 걸리는 중 |
| `batch.gatherPaused` | 보정하는 동안 센서 모으기를 멈췄습니다 |
| `batch.override` | 재실 경고를 무시하고 시작함 |
| `batch.attempt` / `batch.newBatch` | {n}번째 시도 / 새 보정 |
| `batch.pillWaiting` / `batch.pillAt` / `batch.pillRunning` | 보정 {mmss} 후 / 보정 {hhmm} / 보정 중 {done}/{total} |
| `job.idle` / `job.preparing` / `job.starting` / `job.learning` / `job.learningHint` | 시작 대기 / 시작 준비 중… / 시작하는 중… / 학습 중 / 방을 비워 두세요 |
| `job.succeeded` / `job.saved` | 완료 / 보정 기록에 저장했습니다 |
| `job.noReadback` / `job.noReadbackHint` | 완료 (결과값을 읽지 못함) / 전후 비교를 할 수 없습니다 |
| `job.notSaved` / `job.notSavedHint` | 완료 (기록 저장 실패) / 보정은 센서에 반영되었습니다 |
| `job.busy` / `job.busyHint` | 시작하지 못함 (다른 작업 중) / 잠시 뒤 다시 시도하세요 |
| `job.failed` / `job.deviceFailed` / `job.error` | 실패 / 센서가 보정 실패를 알렸습니다. 방을 비우고 다시 시도하세요 / 오류: {error} |
| `job.lostLearning` / `job.lostBefore` | 연결 끊김 (학습 초기화됨) / 연결 끊김 (시작 전) |
| `job.timeout` / `job.timeoutHint` | 응답 없음 / 센서가 완료를 알리지 않았습니다. 다시 시도하세요 |
| `job.cancelled` / `job.cancelledHint` | 취소됨 / 연결을 끊어 학습을 멈췄습니다. 다시 연결하려면 버튼을 누르세요 |
| `retry.title` / `retry.go` | 다시 시도할 센서 ({n}) / 다시 시도 ({n}대) |
| `retry.reconnected` / `retry.whenDone` | 다시 연결됨 · 다시 시도할 수 있습니다 / 이번 보정이 끝난 뒤 다시 시도할 수 있습니다 |
| `retry.pressAgain` / `retry.pressAgainGather` / `retry.startGather` | 버튼을 다시 누르세요 / 센서 모으기를 켜고 버튼을 다시 누르세요 / 센서 모으기 시작 |
| `compare.show` / `compare.title` | 전후 비교 보기 / {alias} 임계값 전후 |
| `compare.zone` / `compare.cell` / `compare.none` | 존 / {before} → {after} ({delta}) / 전후 값이 없습니다 |
| `release.blockedByBatch` | 보정이 끝난 뒤 해제할 수 있습니다 |
| `announce.batchStarted` / `announce.batchDone` / `announce.jobLost` | 보정을 시작했습니다 / 보정이 끝났습니다: 성공 {s}, 실패 {f} / {alias} 보정 중 연결이 끊겼습니다 |
| `error.batch_active` | 이미 진행 중인 보정이 있습니다 |
| `detail.comingSoon` (바뀜) | 설정 편집은 다음 버전에서 제공됩니다 |

`LiveRegion`(9.7절)은 위 `announce.*`를 더 알린다(라운드가 RUNNING이 될 때, 끝날 때, 작업이 `lost`가 될 때). 같은 문구 1초 안 반복 금지는 그대로다.

#### 14.8.11 반응형

| 폭 | 모니터 | 보정 |
|----|--------|------|
| < 640px | 카드 한 열. 존 줄은 라벨 한 줄 + 막대 두 줄(트리거, 유지)로 쌓는다 | 한 열. 주 버튼(다음·시작·다시 시도)은 하단 탭 바 위에 고정, 64px |
| 640–1023px | 카드 한 열, 존 줄은 라벨 · 트리거 · 유지가 한 줄 | 가운데 한 열(최대 640px) |
| ≥ 1024px | 카드 격자 `repeat(auto-fill, minmax(520px, 1fr))` | 두 열: 왼쪽(머리말·카운트다운·시작 방법·취소, sticky), 오른쪽(센서 줄·결과·전후 비교) |

#### 14.8.12 와이어프레임

기호는 10.4절과 같다. 더한 것: `█` 채움(초록), `▓` 채움(임계값 초과, 빨강), `┃` 임계값 tick, `─` 빈칸, `[⏱]` 타이머, `( )` 체크박스·라디오.

**모니터 — 폰 (360px)**

```
+----------------------------------+
| MS605     [~ 보정 중 1/3]   [◐]  |  CalibrationPill (배치가 있을 때만)
+----------------------------------+
| 실시간 모니터                     |
| 볼 센서                           |
| [v 센서 1] [v 센서 2] [ 센서 3 ]   |  토글 칩, 줄바꿈
| (모두 선택) (모두 해제)            |
| ┃ = 임계값(고정) · 막대가 ┃를      |
|   넘으면 빨강 = 임계값 초과         |  범례 (한 번)
| +------------------------------+ |
| | 센서 1             [v] 연결됨 | |
| | [PIR 감지] [S1 재실] [S2 부재] | |
| | [S3 부재]                     | |
| | Z0 · 0.8 m                    | |
| |  재실 트리거 ▓▓▓▓▓┃▓▓──── 64/60| |  넘음: 빨강, 숫자도 빨강
| |  재실 유지   ███──┃────── 22/30| |
| | Z1 · 1.6 m                    | |
| |  재실 트리거 ████─┃────── 41/55| |
| |  재실 유지   █████┃────── 30/30| |
| |  …                            | |
| | Z6 · 5.6 m  꺼짐               | |
| +------------------------------+ |
| | 센서 2          [!] 연결 끊김 | |  투명도 0.6
| | 센서 버튼을 다시 누르세요       | |
| | 실시간 값 없음 (마지막 값)      | |
| +------------------------------+ |
| [=]대시보드 [BT]모으기 [~]모니터 [+]보정 |
+----------------------------------+
```

**모니터 — 데스크톱 (≥1024px)**

```
+-------------------------------------------------------------------------------------------+
| MS605  [대시보드] [센서 모으기] [모니터] [보정]          [⏱ 보정 0:42 후] [시뮬레이터] [◐] |
+-------------------------------------------------------------------------------------------+
| 실시간 모니터    볼 센서: [v 센서 1] [v 센서 2] [v 센서 3] [ 센서 4 ]  (모두 선택) (모두 해제) |
| ┃ = 임계값(고정) · 막대가 ┃를 넘으면 빨강 = 임계값 초과 · 트리거 숫자가 빨간색이면 재실 감지   |
| +------------------------------------------+ +------------------------------------------+ |
| | 센서 1 · 북쪽 벽              [v] 연결됨 | | 센서 2 · 창가    [~] 작업 중 (보정 중)   | |
| | [PIR 없음] [S1 재실] [S2 부재] [S3 부재] | | 보정 중 — 기기가 임계값을 조정하고 있습니다 | |
| | 존         재실 트리거        재실 유지  | | 존         재실 트리거        재실 유지  | |
| | Z0 0.8 m ▓▓▓▓▓┃▓▓── 64/60 ███──┃── 22/30| | Z0 0.8 m ████─┃──── 50/58 ████─┃── 25/28| |
| | Z1 1.6 m ████─┃──── 41/55 █████┃── 30/30| | …                                        | |
| | …                                        | |                                          | |
| | Z6 5.6 m 꺼짐                            | |                                          | |
| +------------------------------------------+ +------------------------------------------+ |
+-------------------------------------------------------------------------------------------+
```

**센서 상세의 실시간 띠 — 폰**

```
| 실시간               (모니터에서 크게 보기) |
| [PIR 없음] [S1 재실] [S2 부재] [S3 부재]   |
| Z0 ▓▓▓▓▓┃▓▓────  64/60                    |  compact: 트리거만
| Z1 ████─┃──────  41/55                    |
| …                                          |
| ( 이 센서 보정 )                            |
| 설정 편집은 다음 버전에서 제공됩니다          |
```

**보정 ① 선택 — 폰**

```
+----------------------------------+
| 다중 자동 보정                    |
| 보정할 센서를 고르세요             |
| 연결된 센서만 고를 수 있습니다      |
| (연결된 센서 모두 선택)            |
| [x] 센서 1             [v] 연결됨 |
| [x] 센서 2             [v] 연결됨 |
| [x] 센서 3             [v] 연결됨 |
| [ ] 센서 4         [!] 연결 끊김  |  비활성
|     연결되어 있지 않음             |
| +------------------------------+ |
| |   다음: 사전점검 (3대)        | |  64px, 하단 고정
| +------------------------------+ |
+----------------------------------+
```

**보정 ② 사전점검 + ③ 시작 방법 — 폰**

```
+----------------------------------+
| < 선택 바꾸기                     |
| 사전점검                (다시 확인)|
| 센서 1   [v] 비어 있음             |
| 센서 2   [!] 아직 사람 있음        |
|          근거: 센서 재실·PIR 감지   |
| 센서 3   [v] 비어 있음             |
|----------------------------------|
| 시작 방법                          |
| [ 지금 ][ N초 후 ][ 시각 예약 ]    |  분할 라디오
|                                  |
| (지금) 고른 경우:                  |
| [ ] 사람이 없는 것을 확인했습니다    |
|     (경고 무시하고 시작)           |  체크해야 시작 가능
| (N초 후) 고른 경우:                |
| [ 30 ]초  (10)(30)(60)(120)       |
| 시작 전에 방을 비워 주세요. 카운트   |
| 다운이 끝나면 보정이 시작됩니다      |
| +------------------------------+ |
| |      30초 후 보정 시작        | |
| +------------------------------+ |
+----------------------------------+
```

**보정 ④ 진행 — 폰 (카운트다운 → 진행)**

```
+----------------------------------+     +----------------------------------+
| MS605     [⏱ 보정 0:42 후]  [◐]  |     | MS605     [~ 보정 중 1/3]   [◐]  |
+----------------------------------+     +----------------------------------+
| 다중 자동 보정                    |     | 다중 자동 보정                    |
|                                  |     | 보정 중 · 1/3 끝남                |
|              0:42                |     | 막대는 예상 시간(약 3:00) 기준입   |
|        후 보정 시작               |     | 니다. 센서는 진행률을 알려 주지     |
|                                  |     | 않습니다.                         |
| [!] 재실 경고를 무시하고 시작함     |     | 보정하는 동안 센서 모으기를 멈췄습니다|
|                                  |     |                                  |
| 센서 1   [o] 시작 대기             |     | 센서 1 [~] 학습 중 · 방을 비워 두세요|
| 센서 2   [o] 시작 대기             |     | [██████████──────────] 1:12 / 약 3:00|
| 센서 3   [o] 시작 대기             |     | 센서 2 [!] 연결 끊김 (학습 초기화됨)|
|                                  |     |   이번 보정이 끝난 뒤 다시 시도할   |
|                                  |     |   수 있습니다                     |
|                                  |     | 센서 3 [v] 완료 · 보정 기록에 저장  |
| +------------------------------+ |     | +------------------------------+ |
| |   카운트다운 취소 (위험 테두리)| |     | |   보정 취소 (확인 다이얼로그)  | |
| +------------------------------+ |     | +------------------------------+ |
+----------------------------------+     +----------------------------------+
```

**보정 ⑤ 결과 + 재시도 — 폰**

```
+----------------------------------+
| 다중 자동 보정                    |
| 보정 끝 · 성공 2 · 실패 1          |
| 센서 1  [v] 완료                  |
|   보정 기록에 저장했습니다          |
|   > 전후 비교 보기                 |
|     존  재실 트리거     재실 유지   |
|     Z0  70 → 64 (−6)  30 → 28 (−2) |
|     Z1  62 → 66 (+4)  30 → 30 (0)  |
|     …                             |
| 센서 2  [!] 연결 끊김 (학습 초기화됨)|
| 센서 3  [v] 완료                  |
|----------------------------------|
| 다시 시도할 센서 (1)               |
| [ ] 센서 2                        |  아직 끊김: 체크 불가
|     센서 모으기를 켜고 버튼을       |
|     다시 누르세요                  |
|     ( 센서 모으기 시작 )           |
| 시작 방법 [ 지금 ][ N초 후 ]  30초  |
| ( 새 보정 )                       |
| +------------------------------+ |
| |      다시 시도 (0대)  (비활성)  | |  버튼을 누르고 다시 연결되면 1대
| +------------------------------+ |
+----------------------------------+
```

**보정 — 데스크톱 (진행 중, 재시도 라운드)**

```
+-------------------------------------------------------------------------------------------+
| MS605  [대시보드] [센서 모으기] [모니터] [보정]                    [~ 보정 중 0/1] [◐]      |
+-------------------------------------------------------------------------------------------+
|  +-----------------------------------+   센서                                               |
|  | 다중 자동 보정                     |   +-----------------------------------------------+ |
|  | 보정 중 · 0/1 끝남                 |   | 센서 1   [v] 완료       > 전후 비교 보기       | |  이번 라운드 아님: 흐리게
|  | 막대는 예상 시간(약 3:00) 기준입니다.|   | 센서 2   [~] 학습 중 · 2번째 시도             | |
|  | 센서는 진행률을 알려 주지 않습니다.  |   |   [████████────────────] 0:58 / 약 3:00       | |
|  | 보정하는 동안 센서 모으기를 멈췄습니다|   | 센서 3   [v] 완료       > 전후 비교 보기       | |
|  |                                   |   +-----------------------------------------------+ |
|  | ( 보정 취소 )                      |                                                     |
|  +-----------------------------------+                                                     |
+-------------------------------------------------------------------------------------------+
```

### 14.9 테스트

M2 11장의 공통 규칙(시뮬레이터, `Storage(root=tmp_path)`, 건너뛰기·xfail·빈 테스트 금지, WS 테스트에 `@pytest.mark.timeout(30)`, `no_chunk_pacing`)을 그대로 따른다.
공통 픽스처도 같다(`SimFleet(3, speed=100)`, `keepalive_interval=0.15`, `gather_pause=0.01`). `create_app(..., sim=sim)`이면 배치의 `timeout`과
`expected_s`가 속도 100으로 나뉜다(보정 1.8초). 사전점검은 `window_s=0.3`. 순서 검사를 하는 도우미는 `seq is None`인 메시지를 먼저 거른다.

#### 14.9.1 백엔드

| 파일 | 꼭 검증할 것 |
|------|--------------|
| `test_gui_live.py` | **구독 = 참조**: 클라이언트 A가 `live_subscribe [SIM1]` → 시뮬레이터 `tags[TAG_LIVE_OUTPUT_ENABLE] == b"\x01"`이고 세션 live 카운트 1. B도 구독 → 카운트는 여전히 1(G12). A 해제 → 켜진 채. B 해제 → tag54 꺼짐, 카운트 0. **WS 끊김**: 구독한 채 연결을 닫으면 꺼짐. 모으기 전에 구독 → 센서가 모이면 켜지고 `live`가 온다. 해제·재수집(`sim/drop` → `press`)·`release` 뒤에도 카운트가 0 또는 1을 넘지 않음(새는 참조 없음). **내용**: `live`의 `seq is None`, `zones` 7개, `distance_m == [0.8, 1.6, 2.4, 3.2, 4.0, 4.8, 5.6]`(시뮬레이터 tag53. `FALLBACK_DISTANCES_M`과 다르므로 출처가 구별된다), `sub_sensor_presence` 3개, 시뮬레이터 `signal`을 임계값 위로 올리면 `trigger > trigger_threshold`와 `trigger_active`. **조절**: 속도 100(tag55 100 Hz)에서 1초 동안 한 센서의 `live`가 6개 이하이고 2개 이상. **구독 안 함**: 구독하지 않은 클라이언트는 `live`를 받지 않고, 그 클라이언트의 `seq` 열은 끊김 없이 이어진다. **잘못된 프레임**: `{"type":"nope"}`, 깨진 JSON, 4097바이트 프레임 → 연결 유지, 이후 정상 구독이 동작. **`Client` 단위**(가짜 소켓: `send_text`가 이벤트를 기다리며 멈춘다. `TestClient`의 전송은 밀리지 않으므로 느린 클라이언트는 이렇게만 재현된다): 멈춘 동안 같은 키에 `put_slot` 50번 → 풀면 그 키는 마지막 값 하나만 나감, `maxsize=8`인데 슬롯 100번 → 닫히지 않음(1013 아님), 큐 메시지가 슬롯보다 먼저 나감, 두 키는 번갈아 나감(덮어쓴 키가 뒤로 감), `close()` 뒤 `put_slot`은 무시 |
| `test_gui_batch.py` | **수명주기**: 3대 모으기 → `POST /api/batches {device_ids, start:"now"}` 202 → `batch`(waiting 또는 running) → 센서마다 `calibration_job`이 `starting` → `learning`(→ `elapsed_s` 증가) → `succeeded` 부분열 → `batch` done. 결과의 `before`/`after` 7쌍, `history_saved`, `calibration_history.jsonl` 3줄과 각 `device_id`, 끝난 뒤 `sensor.last_calibration`이 채워짐. **M3 완료 기준**: 이 테스트만 `SimFleet(7)`, 학습 중 1대 `sim/drop` → 그 작업 `lost`·`started: true`, 나머지 6대 `succeeded`. 두 WS 클라이언트가 같은 `(seq, type, data)` 열을 받음(`seq` 정수만). **재시도**: 끊긴 센서가 아직 LOST면 `retry {}` → 409 `not_connected`, `retry {device_ids:[성공한 센서]}` → 422 `invalid`. `gather/start` + `sim/press` → 다시 연결 → `retry {start:"now"}` 202 → 같은 `batch_id`, `round == 2`, `round_ids == [그 센서]`, 그 작업 `attempt == 2` → `succeeded`. 재시도하지 않은 센서의 결과는 그대로. **모으기 정지(G20)**: 모으는 중에 발사하면 RUNNING 뒤 `gather.gathering == false`. **카운트다운 취소**: `start:"delay", delay_s:5` → `countdown`(`seq: null`, `remaining_s` ≤ 5, 줄어듦)을 받음 → `cancel` 200 → `cancelled`, 모든 작업 `cancelled`·`started: false`, 시뮬레이터에 tag52 쓰기 없음(`frames_in`), 링크는 연결된 채. **RUNNING 취소**: 200, 학습 중이던 센서는 `cancelled`·`started: true`, 세션 `disconnected`. **예약 + 빠른 시계**: `monkeypatch.setattr(ms605.fleet, "time", SimpleNamespace(time=lambda: time.time() + offset[0]))`, `start:"at"`, `at = 지금 + 1시간`, 실제 1초 기다리는 동안 배치 `waiting`이고 링크 CONNECTED(keep-alive가 유지, 유휴 끊김 0.3초보다 훨씬 김) → `offset[0] = 3600` → 1초 남짓 안에 running(코어가 1초마다 시계를 다시 읽는다) → done. **늦게 붙은 화면**: 라운드 진행 중에 새로 연결한 클라이언트의 스냅샷 `batch`가 running이고 작업 상태가 앞선 클라이언트의 마지막 `calibration_job`과 같다. 그 스냅샷의 `seq` 다음부터 이어진다. **한 번에 하나**: 진행 중 두 번째 `POST /api/batches` → 409 `batch_active`. **작업 잠금**: 진행 중 그 센서 `release` → 409 `batch_active`(링크 유지), `release {}` → 409, 보정 중 그 센서 `preflight` → 409 `busy`. **검증**: `start:"delay"`에 `delay_s` 없음, `start:"now"`에 `delay_s`, 시간대 없는 `at`, 중복 id, 빈 목록, 모르는 필드 → 422 `invalid_request`. 지난 `at`, 24시간 넘는 `at` → 422 `invalid`. 없는 세션 → 404, 연결 끊긴 센서 → 409 `not_connected`. `GET /api/batches/<다른 id>` → 404. **사전점검**: `signal`을 임계값 위로 올린 센서는 `occupied: true`·`presence: true`, 나머지 `false`, LOST 센서는 `error == "not connected"`, 순서는 요청 순서. **identify 대기(M3 항목 3, 라우트 쪽)**: 앱 루프에서 `async with session.operation("identify")`를 0.3초 잡는 태스크를 띄운 직후(`TestClient.portal.start_task_soon`) `POST /api/batches {start:"now"}` → 409가 아니라 202(4단계가 `"identify"`를 막지 않음)이고 그 작업은 `failed`(`busy: identify`)가 아니라 `succeeded`(코어가 기다림). 재수집 직후 발사 자체의 회귀는 코어 `test_fleet.py`가 맡는다 |
| `test_gui_meter_parity.py` | `web/src/test/meter_cases.json`의 모든 사례에서 `_ui.meter(value, threshold, width=, tick_at=)`의 `plain`과 "채움 칸에 `bar.active`가 있는가"가 파일의 `plain`, `over`와 같다. 사례가 20개 이상이고 0·음수 임계값을 포함한다 |
| `test_gui_ws.py` (수정) | 이벤트 덮개 테스트가 새 `HANDLED_EVENTS`/`IGNORED_EVENTS`로 통과. `seq` 연속 검사는 정수 `seq`만 |
| `test_gui_schema.py` (수정) | `ServerMessage` 11개, `ClientMessage` 2개를 `type`으로 구별. `LiveMessage`/`CountdownMessage`의 `seq`가 JSON 스키마에서 `{"type": "null"}`이고 필수. `web/openapi.json`이 최신 |
| 코어 | `docs/CORE_API.md` 14장 표의 M3 행(`test_calibration.py` identify 대기, `test_session.py` tag53, `test_fleet.py` 재수집 직후 발사) |

#### 14.9.2 프런트엔드 (vitest)

- `meter.test.ts`: `meter_cases.json`의 모든 사례에서 `meterPlain(meter(...))`와 `over`가 같다(파이썬과 같은 파일). 14.8.4절에 적은 `tests/test_ui.py`의 경우들,
  `cells[tickAt] === 'tick'`이 임계값과 관계없음, `RangeError` 4경우.
- `reducer.test.ts`(더함): 스냅샷이 `batch`와 `countdown`(`fire_at - ts`)을 정하고 `live`를 비움, `batch`가 바꾸고 waiting이면 `countdown`을 다시 정함·아니면 `null`,
  `calibration_job`은 같은 `batch_id`에서만 그 작업을 바꾸고 순서를 지킴·다른 id면 무시하지만 `lastSeq`는 오름, `live`는 `watch`가 있는 id만·`lastSeq`를 건드리지 않음,
  `countdown`은 waiting이 아니거나 id가 다르면 무시, `removeWatch`가 0이 되면 `live[id]`도 지움, `addWatch` 두 번 + `removeWatch` 한 번이면 남음.
- `calibration.test.ts`: 14.8.9절 세 표의 모든 행(`label`이 항상 비어 있지 않음), LOST hint 네 갈래, `jobProgress`(0.99 상한, `overdue`, 성공 1, 그 밖 `null`),
  `formatCountdown`(59.2 → `1:00`, 3600 → `1:00:00`), `formatElapsed`(72.9 → `1:12`), `resolveAt`(지금 뒤의 오늘 / 지난 시각은 내일 / 자정 넘김), `batchHeadline` 6행.
- `ws.test.ts`(더함): 스냅샷 전에는 아무것도 보내지 않음, 스냅샷 뒤 `watch`의 모든 id로 `live_subscribe` 한 번, `watch` 변화가 차이만 보냄(구독·해제),
  33개 id는 32 + 1로 나뉨, 재접속 뒤 새 스냅샷에서 다시 전부 구독.
- 컴포넌트 스모크(fixture 스토어, `fetch` 모의):
  - `Meter`: `data-cell` 열이 `meter()`와 같고, `aria-label`에 `초과`는 `over`일 때만, `data-hot`.
  - `MonitorScreen`: 고른 센서마다 카드와 존 7줄, 꺼진 존은 `꺼짐`, LOST 센서 카드는 hint와 `실시간 값 없음`, 칩을 끄면 `watch`에서 빠짐, 세션이 없으면 빈 상태.
  - `LiveStrip`: 마운트하면 `watch[id] === 1`, 언마운트하면 0.
  - `CalibrateScreen`: 선택 → `다음` → `preflight` 호출(본문 `device_ids`) → `아직 사람 있음`이 보이고 `지금`에서는 체크 전 시작 버튼 비활성 → 체크 후 `createBatch`가
    `presence_override: true`로 불림. `N초 후`에서는 체크박스 없이 시작 가능하고 본문이 `{start:"delay", delay_s:30}`. fixture `batch`가 running이면 어느 단계든 `BatchProgress`와
    `batch.progressNote`, waiting이면 카운트다운 문구와 `카운트다운 취소` → `cancelBatch`(확인 없음), running의 `보정 취소`는 확인 다이얼로그를 거침. done이면 결과,
    전후 비교 표의 `70 → 64 (−6)`, LOST 센서의 hint와 (모으기 꺼짐이면) `센서 모으기 시작`, 다시 연결된 센서만 체크 가능하고 `retryBatch` 본문의 `device_ids`.
    409 `batch_active` → `이미 진행 중인 보정이 있습니다`.
  - `CalibrationPill`: waiting/running/없음 세 상태.
- fixture는 `test/fixtures.ts`에 합성 값으로 더한다(`BatchView` 3대: 성공·LOST·성공, `LiveData` 한 장: 존 하나는 임계값 위, 하나는 꺼짐, 하나는 음수 임계값).

#### 14.9.3 e2e (`tests/test_gui_e2e.py`에 함수 하나 더)

실제 프로세스로 3대 배치를 HTTP + WS로 끝까지 돌린다. 30초 안이어야 한다(`@pytest.mark.timeout(60)`).

1. M2 e2e의 1~3단계와 같되 `--sim 3 --speed 40`(보정 4.5초, keep-alive 0.375초, 버튼 창 3초).
2. WS 클라이언트 둘(A, B). `POST /api/gather/start`, `sim/press/1..3` → 세 센서 CONNECTED.
3. A만 `live_subscribe [SIM1]` → A가 `seq: null`인 `live`(존 7개, `distance_m[0] == 0.8`)를 받고, B는 다음 단계까지 `live`를 받지 않는다.
4. `POST /api/preflight {device_ids: [3대], window_s: 0.5}` → 3개, 모두 `occupied: false`.
5. `POST /api/batches {device_ids, start:"delay", delay_s: 1}` → 202. A·B 둘 다 `batch`(waiting)와 `countdown`을 받는다. 곧 `batch` running, `gather.gathering == false`(G20).
6. 센서 2의 `calibration_job`이 `learning`이 되면 `POST /api/sim/drop/2`.
7. `batch` done: 센서 1·3 `succeeded`(`before`/`after` 있음), 센서 2 `lost`·`started: true`·`retryable: true`.
8. `POST /api/batches/{id}/retry {}` → 409 `not_connected`. `POST /api/gather/start`, `POST /api/sim/press/2` → 센서 2 CONNECTED.
9. `POST /api/batches/{id}/retry {"start": "now"}` → 202, `round == 2`. `batch` done, 센서 2 `succeeded`·`attempt == 2`, 센서 1·3은 그대로.
10. `tmp_path/cal_results/sim/calibration_history.jsonl`이 3줄이고 `device_id`가 세 센서와 같다.
11. A·B가 받은 메시지 중 `seq`가 정수인 것의 `(seq, type)` 열이 같고 끊김이 없다(D11, M3 완료 기준 "폰에서 시작한 보정이 노트북에도 보인다").
12. `SIGINT` → 10초 안에 종료, 종료 코드 0 또는 130.

#### 14.9.4 명령

11.3절과 같다. 더해서 `git status --short web/src/test/meter_cases.json`이 깨끗해야 한다(파이썬과 TS가 같은 커밋본을 본다).

### 14.10 통합 순서와 완료 기준

1. (코어) `docs/CORE_API.md` 2.2절 세 가지와 그 테스트. `pytest -q`, `ruff check .`.
2. (백엔드) `schemas.py`를 14.4절 그대로, `python -m ms605.gui.schemas web/openapi.json`. 그 뒤 프런트엔드는 `npm run typegen`.
3. (병행) 백엔드: `live.py`, `batch.py`, `ws.py`·`server.py` 변경과 테스트. 프런트엔드: `meter.ts`·`meter_cases.json`(14.8.4절 표), `calibration.ts`, 스토어·WS,
   화면·컴포넌트, vitest. 프런트엔드는 2가 끝나기 전에는 14.4절을 보고 fixture로 작업한다.
4. (통합) `uv run ms605 gui --sim 7 --speed 20`으로 노트북과 폰에서 확인하고 `npm run build` 결과를 커밋 대상에 넣는다.

M3 완료 기준(GUI_PLAN): `ms605 gui --sim 7`에서 7대를 모아 보정하는 도중 1대를 `끊기`로 끊어도 나머지 6대가 성공하고, 끊긴 센서만 버튼을 다시 눌러 재시도해 성공한다.
폰에서 시작한 보정(카운트다운 포함)이 노트북 화면에도 같은 진행·결과로 보이고, 어느 쪽에서든 취소할 수 있다. 모니터 화면에서 여러 센서의 막대가 CLI와 같은 의미로
움직이고, 화면을 닫으면 그 센서의 실시간 출력이 꺼진다. 전체 pytest, ruff, `npm run typecheck`, `npm test`, `npm run build`가 통과한다.

### 14.11 범위 밖 (M4+)

- 초안 편집·차이 미리보기·적용·검증·되돌리기·클론, 임계선 드래그, 존 활성화 편집, 상대값 일괄 정책(M4). 모니터 막대는 보기 전용이다.
- 고급 설정(서브센서 존·타이밍, DND, 시간 동기화), 보정 이력 조회 화면과 `GET /api/sensors/{id}/history`(M4, 6.7절에서 옮김)
- 배치 여러 개 동시 실행, 배치 기록(지난 배치 목록). 서버는 마지막 배치 하나만 기억하고, 서버를 다시 띄우면 잊는다(결과는 `calibration_history.jsonl`에 남는다)
- 발사 시각의 자동 재실 재확인, 시뮬레이터의 재실 조작 API(테스트는 `SimMS605.signal`을 직접 바꾼다)
- 노트북 잠자기 방지(`caffeinate`), 예약 보정의 서버 재시작 복구
- 보정 취소 동작·보정 중 keep-alive와 스캔의 영향·실제 보정 시간의 실기기 검증(M5)
