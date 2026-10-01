# MS605 코어 API (M1)

`docs/GUI_PLAN.md`의 M1(코어 추출)을 구현하기 위한 계약 문서다. 다음 구현자는 이 문서의
클래스·시그니처·상태 머신·오류 계약을 그대로 구현하고, M2의 FastAPI/WebSocket 서버는 이 표면을
그대로 노출한다. 설계 결정 D1~D15와 3장 목표 구조를 전제로 하며, 여기서 새로 정한 것은 이유를 함께 적는다.

이 저장소는 공개 저장소다. 이 문서와 테스트의 예시 값(Device ID, 주소, 사이트 이름, 호스트 이름)은
모두 합성 값이며, 실제 값은 어디에도 쓰지 않는다.

## 1. 근거가 되는 수치

코어의 기본값은 아래 수치에서 나온다. 모든 시간은 생성자 인자(벽시계 초)로 바꿀 수 있어야 한다.
시뮬레이터를 `speed`로 빠르게 돌리는 테스트가 이 값을 줄여 쓰기 때문이다(14장).

| 상수 | 위치 | 값 | 근거 |
|------|------|----|------|
| `KEEPALIVE_INTERVAL_S` | `session.py` | 15.0 | 유휴 끊김 29.6초(1회), 25초 주기는 유지·30초는 끊김. 현재 15초는 안전 |
| `RECONNECT_TIMEOUT_S` | `session.py` | 120.0 | 버튼 후 연결 가능 시간은 117초 이상(하한). 사람이 버튼을 누르기를 기다리는 상한 |
| `RELEASE_TIMEOUT_S` | `session.py` | 5.0 | 기존 CLI의 bounded disconnect와 같다 |
| `VERIFY_TIMEOUT_S` / `VERIFY_INTERVAL_S` | `fleet.py` | 3.0 / 0.4 | tag51 쓰기는 즉시 읽으면 이전 값, 1초 뒤에는 반영. 기존 `confirm_profile_applied` 값 유지 |
| `EXPECTED_CALIBRATION_S` | `calibration.py` | 180.0 | **미측정**. GUI_PLAN 1장 "최대 약 3분", 시뮬레이터 가정과 같다. 진행 막대의 분모로만 쓴다 |
| `CALIBRATION_TIMEOUT_S` | `protocol.py`(기존) | 200.0 | 기존 값 유지 |
| `PREFLIGHT_WINDOW_S` | `calibration.py` | 3.0 | tag55 주기 약 1초(시뮬레이터 가정) → 표본 약 3개 |
| `IDENTIFY_WAIT_S` | `calibration.py` | 2.0 | (M3) 재수집 직후의 `"identify"` 잠금은 tag 읽기 한 번(응답 대기 최대 `WRITE_TIMEOUT_S`보다 훨씬 짧다)이다. 보정이 그 잠금을 기다리는 상한(5.2절) |
| `GATHER_PAUSE_S` | `fleet.py` | 1.0 | 기존 `--collect` 스캔 루프의 쉬는 시간 |

측정 결과 중 설계에 직접 반영한 것:

- tag61과 tag51은 BLE로 관찰한 범위에서 독립이다. 그래서 초안 적용은 쓰기 순서에 기대지 않는다.
  `apply_profile()`의 "민감도 먼저" 순서는 예방 조치로 그대로 둔다.
- tag51 쓰기는 즉시 재조회에 보이지 않는다. 그래서 모든 쓰기 검증은 **폴링**한다(6.4절).
- tag30은 20바이트이고 기기마다 고유했다(1대, 약한 근거). 영구 키로 쓰되, 같은 ID가 두 링크에서
  동시에 나타나는 경우를 6.1절에서 정의해 둔다.
- 동시 연결은 2대까지만 확인됐다. 코어는 연결 수를 제한하지 않는다(10장).
- 보정 중 keep-alive의 영향과 실제 보정 시간은 **미측정**이다. 그래서 보정 중에는 지금 실기기에서
  동작이 확인된 방식(드라이버의 15초 keep-alive 하나만)을 그대로 쓴다(4.4절).

## 2. 모듈 구성과 공통 규칙

```
ms605/
  errors.py       (+ SessionBusyError, StorageError)
  models.py       (+ FALLBACK_DISTANCES_M, zone_distances  ← cli.py에서 이동)
  driver.py       (+ start_auto_calibration(on_started=...))
  events.py       이벤트 데이터클래스, 상태 enum, EventBus
  session.py      DeviceSession, DeviceInfo
  calibration.py  CalibrationJob, PresenceSnapshot, presence_snapshot(), build_calibration_record()
  fleet.py        Fleet, BatchCalibration, ThresholdChange, SensorChanges, Draft, apply_changes(), poll_verify()
  registry.py     Registry, Site, Sensor, PendingSensor, MatchResult
  storage.py      data_root(), Storage, Snapshot
```

의존 방향은 한쪽으로만 흐른다. 순환 import가 없어야 한다.

```
models ← events ← session ← calibration ← fleet
                   storage ← registry  ←  fleet
                   storage ← calibration
```

공통 규칙:

- 코어(`ms605/*.py`, `ms605/cli` 제외)는 `print`하지 않고, `rich`/`questionary`/`ms605.cli`를 import하지 않으며,
  터미널을 건드리지 않는다. 진단은 `logging.getLogger(__name__)`만 쓴다.
- asyncio만 쓴다. 스레드, `run_in_executor`, `asyncio.to_thread`를 쓰지 않는다. 저장 파일은 작으므로
  동기 파일 I/O를 이벤트 루프에서 그대로 한다.
- Python 3.10을 지원한다. `asyncio.timeout`, `TaskGroup`, `StrEnum`은 3.11+이므로 쓰지 않는다.
  enum은 `class X(str, Enum)`, 타임아웃은 `asyncio.wait_for`를 쓴다. `@dataclass(kw_only=True)`는 3.10에서 쓸 수 있다.
- `CancelledError`를 삼키지 않는다. 기존 코드처럼 정리 후 다시 올린다. 백그라운드 태스크를 끝낼 때는
  `task.cancel()` 후 `await asyncio.gather(task, return_exceptions=True)`를 쓴다.
- 새 모듈은 `ms605/__init__.py`에서 다시 내보내지 않는다(`from ms605.fleet import Fleet`처럼 직접 import).
  최상위에는 새 예외 두 개만 추가한다.
- 플러그인, 범용 작업(job) 프레임워크, 이 문서에 없는 설정 옵션은 만들지 않는다.

### 2.1 기존 모듈의 작은 변경

| 파일 | 변경 | 이유 |
|------|------|------|
| `errors.py` | `SessionBusyError(MS605Error)`: `reason: str` 속성. `StorageError(MS605Error)` | 9장 오류 계약 |
| `models.py` | `FALLBACK_DISTANCES_M`, `zone_distances(cfg)`를 `cli.py`에서 그대로 옮긴다 | 보정 기록(`build_calibration_record`)이 코어로 오기 때문 |
| `driver.py` | `start_auto_calibration(..., on_started: Callable[[], None] \| None = None)`. tag52=4의 ACK를 받고 오래된 tag62 검사를 마친 직후 한 번 호출한다. 콜백 예외는 로그만 남긴다 | `CalibrationJob`이 STARTING → LEARNING 전이를 알 수 있는 유일한 지점 |

### 2.2 M3에서 더한 것

GUI M3(`docs/GUI_API.md` 14장)가 코어에 요구하는 변경은 아래 세 가지뿐이다. 그 밖의 M3 기능은 GUI 서버가 이 문서의 기존 표면을 감싸서 만든다.

| 파일 | 변경 | 이유 |
|------|------|------|
| `session.py` | `DeviceInfo`에 마지막 필드 `zone_distances_m: tuple[float, ...] \| None = None`을 더한다. `read_info()`는 같은 `read_raw()` 한 번에 tag53(`TAG_ZONE_DISTANCES`)도 읽고, 값이 있으면 `decode_zone_distances(value)`, 없거나 비면 `None`을 넣는다(4.1절) | GUI 실시간 막대가 존 거리를 표시한다. 따로 `operation("read")`로 설정을 읽으면 그 순간 발사된 배치가 `busy: read`로 실패하므로, 이미 잡는 `"identify"` 읽기에 tag 하나를 더한다. 기본값이 있으므로 기존 생성 코드는 그대로다 |
| `models.py` | `zone_distances(cfg)`의 본문은 그대로 두고, 인자 타입만 `HasZoneDistances`로 넓힌다: `class HasZoneDistances(Protocol)`에 읽기 전용 속성 `zone_distances_m: Sequence[float] \| None` 하나. `MS605Config`와 `DeviceInfo`가 둘 다 맞는다(`None`이면 `FALLBACK_DISTANCES_M`) | `models`는 `session`을 import할 수 없다(2장 의존 방향). 본문이 이미 `cfg.zone_distances_m or ()`만 읽으므로 동작은 같다 |
| `calibration.py` | `IDENTIFY_WAIT_S = 2.0`, `CalibrationJob(..., identify_wait: float = IDENTIFY_WAIT_S)`. `run()`이 `"identify"` 잠금만은 이 시간까지 기다린다(5.2절 첫 행) | M1의 열린 문제: 재수집 직후 세션이 잠깐 `"identify"`를 잡고 있는데, 그 사이 발사된 배치가 곧바로 `FAILED("busy: identify")`가 됐다 |

## 3. `events.py`

### 3.1 상태 enum

모든 enum은 `(str, Enum)`이고 값은 소문자 이름이다(예: `LinkState.CONNECTED.value == "connected"`).
M2에서 JSON으로 그대로 직렬화된다.

```python
class LinkState(str, Enum):        DISCONNECTED, CONNECTING, CONNECTED, LOST
class CalibrationState(str, Enum): IDLE, STARTING, LEARNING, SUCCEEDED, FAILED, LOST, TIMEOUT, CANCELLED
class BatchState(str, Enum):       WAITING, RUNNING, DONE, CANCELLED
class ApplyStatus(str, Enum):      OK, PARTIAL, UNVERIFIED, FAILED
```

`CalibrationState`의 종료 상태는 `SUCCEEDED, FAILED, LOST, TIMEOUT, CANCELLED`다.
`TERMINAL_CALIBRATION_STATES: frozenset[CalibrationState]`로 노출한다.

### 3.2 이벤트 데이터클래스

모두 `@dataclass(frozen=True, kw_only=True)`다. 필드는 `dataclasses.asdict()` 후 `json.dumps()`가 되는
타입만 쓴다: 기본형, `str` enum, tuple, `None`, `ms605.models`의 데이터클래스. M2는
`{"type": type(ev).__name__, **asdict(ev)}`로 보낸다.

```python
class Event:
    at: float = field(default_factory=time.time)    # 발생 시각, epoch 초

class DeviceEvent(Event):
    address: str              # 이 호스트의 BLE 주소
    device_id: str | None     # tag30 소문자 hex. 식별 전에는 None
```

| 이벤트 | 추가 필드 | 보내는 곳 | 언제 |
|--------|-----------|-----------|------|
| `LinkStateChanged(DeviceEvent)` | `state: LinkState`, `previous: LinkState`, `reason: str = ""` | session | 링크 상태가 바뀔 때마다(4.2절 표) |
| `KeepAliveMissed(DeviceEvent)` | `error: str`, `kind: str`(`"error"`: 기기가 오류 상태로 응답, `"no_response"`: ACK 없음) | session | keep-alive가 실패했지만 링크는 살아 있다고 판단할 때(4.4절) |
| `BusyChanged(DeviceEvent)` | `busy: str \| None` | session | 작업 잠금을 잡을 때(이유)와 놓을 때(`None`) |
| `LiveRadar(DeviceEvent)` | `snapshot: RadarOutputSnapshot` | session | 디코딩에 성공한 tag55 push마다 |
| `PirChanged(DeviceEvent)` | `detected: bool` | session | tag56 값이 직전과 다를 때. 새 스트림의 첫 값은 항상(4.5절) |
| `FrameDropped(DeviceEvent)` | `tag: int`, `reason: str` | session | push를 디코딩하지 못해 버렸을 때(예: 짧은 tag55) |
| `SensorGathered(DeviceEvent)` | `name: str \| None`, `known: bool`, `site_id: str \| None`, `alias: str \| None`, `resolved_pending: bool` | fleet | 새 링크를 연결하고(수집 루프가 LOST/DISCONNECTED 세션을 다시 붙인 경우도) tag30을 읽어 레지스트리와 맞춘 뒤. `device_id`는 항상 있다 |
| `GatherFailed(DeviceEvent)` | `name: str \| None`, `error: str` | fleet | 수집 중 연결 또는 식별이 실패했을 때 |
| `CalibrationStateChanged(DeviceEvent)` | `state`, `previous: CalibrationState`, `detail: str = ""` | calibration | 상태 전이마다(5.2절 표) |
| `CalibrationProgress(DeviceEvent)` | `elapsed_s: float`, `expected_s: float` | calibration | LEARNING 동안 `progress_interval`마다. 기기가 보낸 진행률이 아니다(D6) |
| `CalibrationResult(DeviceEvent)` | `state`, `started: bool`, `before: tuple[tuple[int, int], ...] \| None`, `after: ... \| None`, `error: str \| None`, `detail: str = ""`, `history_saved: bool = False` | calibration, fleet | 작업이 종료 상태에 들어갈 때 한 번. `CalibrationJob.run()`의 반환값과 같은 객체 |
| `BatchChanged(Event)` | `batch_id: str`, `state: BatchState`, `fire_at: float`, `device_ids: tuple[str, ...]` | fleet | 배치 생성(WAITING)과 상태 전이마다 |
| `ApplyResult(DeviceEvent)` | `reason: str`, `status: ApplyStatus`, `applied`, `skipped`, `mismatched: tuple[str, ...]`, `snapshot: str \| None`, `error: str \| None` | fleet | 기기 하나의 적용·되돌리기가 끝날 때. `apply_changes()`의 반환값과 같은 객체 |
| `HandlerFailed(Event)` | `event_type: str`, `handler: str`, `error: str` | bus | 구독 콜백이 예외를 냈을 때 |

결과형 이벤트(`CalibrationResult`, `ApplyResult`)는 반환값과 같은 객체다. 반환값 타입을 따로 두지
않으면 CLI와 GUI가 같은 정보를 같은 모양으로 받는다.

`CalibrationResult.started`는 tag52=4를 보냈는지다. 이 값으로 "대기 중 끊김"(`LOST`, `started=False`)과
"보정 중 끊김"(`LOST`, `started=True`)을 구별한다. CLI의 `lost`와 `calibration_lost`가 이 둘이다(12.3절).

### 3.3 `EventBus`

```python
class EventBus:
    def subscribe(self, callback: Callable[[Event], None]) -> Callable[[], None]: ...
    def stream(self, *, maxsize: int = 256) -> EventStream: ...
    def emit(self, event: Event) -> None: ...

class EventStream:   # async iterator + async context manager
    dropped: int     # 큐가 가득 차서 버린 이벤트 수
    def close(self) -> None: ...
    def __aiter__(self) -> EventStream: ...
    async def __anext__(self) -> Event: ...      # close() 뒤에는 StopAsyncIteration
    async def __aenter__(self) -> EventStream: ...
    async def __aexit__(self, *exc: object) -> None: ...   # close() + 구독 해제
```

- `emit()`은 동기 함수다. 드라이버 push 콜백(동기 컨텍스트)에서도 부를 수 있어야 하기 때문이다.
  구독 순서대로 콜백을 즉시 호출하고, 스트림 큐에 `put_nowait`한다.
- 콜백은 동기 함수여야 한다. 비동기 소비자는 `stream()`을 쓴다.
- 디스패치는 구독자 목록의 복사본을 순회한다. 그래서 콜백 안에서 구독을 해제해도 다른 구독자를 건너뛰지 않는다.
- 콜백 안에서 `emit()`하면 재귀적으로 즉시 디스패치된다(큐잉하지 않는다).
- **예외 격리**: 콜백이 예외를 내면 `_log.exception()`으로 남기고 `HandlerFailed`를 보낸 뒤 다음 구독자로
  넘어간다. `HandlerFailed`를 처리하던 콜백이 예외를 내면 로그만 남긴다(무한 재귀 방지).
- **느린 소비자**: 스트림 큐가 가득 차면 가장 오래된 이벤트를 버리고 새 이벤트를 넣으며 `dropped`를 1 늘린다.
  1초에 기기당 한 번 오는 `LiveRadar` 때문에 멈춘 소비자가 메모리를 키우지 않게 한다. 상태 이벤트도
  버려질 수 있으므로, 소비자는 다시 붙을 때 `Fleet`/`DeviceSession`의 현재 상태를 직접 읽는다(13장).
- 버스는 이벤트 루프 하나에서만 쓴다. 스레드 안전하지 않다.

## 4. `session.py`

### 4.1 공개 표면

```python
@dataclass(frozen=True)
class DeviceInfo:
    device_id: str                    # tag30 소문자 hex
    battery_pct: int | None           # tag23 첫 바이트
    version: tuple[int, ...] | None   # tag21, decode_supported_tags()
    light_lux: int | None             # tag36, big-endian 정수
    zone_distances_m: tuple[float, ...] | None = None   # (M3) tag53, decode_zone_distances(). 없으면 None

class DeviceSession:
    def __init__(
        self,
        device: BLEDevice | str,
        bus: EventBus,
        *,
        scan: Callable[[float], Awaitable[list[BLEDevice]]] | None = None,   # 기본 MS605.scan
        client_factory: Callable[..., object] | None = None,                 # MS605(client_factory=)에 전달
        keepalive_interval: float = KEEPALIVE_INTERVAL_S,
        connect_timeout: float = 10.0,
        scan_secs: float = 5.0,
    ) -> None: ...

    # 읽기 전용 상태
    bus: EventBus
    ms: MS605                          # 4.3절 규칙 안에서만 쓴다
    address: str
    name: str | None                   # BLE 광고 이름
    state: LinkState                   # 처음에는 DISCONNECTED
    busy: str | None                   # 작업 잠금 이유, 비어 있으면 None
    device_id: str | None              # read_info() 후에 채워진다
    info: DeviceInfo | None
    last_radar: RadarOutputSnapshot | None
    last_pir: bool | None              # 지금 스트림에서 본 마지막 값(4.5절)
    last_lost_reason: str              # 마지막으로 LOST가 된 이유. 연결에 성공하거나 close()하면 ""
    keepalive_interval: float

    async def connect(self, device: BLEDevice | None = None) -> None: ...
    async def reconnect_once(self) -> None: ...
    async def reconnect(self, *, timeout: float = RECONNECT_TIMEOUT_S) -> None: ...
    async def close(self) -> None: ...
    async def read_info(self) -> DeviceInfo: ...
    def operation(self, reason: str, *, suspend_keepalive: bool = False) -> AsyncContextManager[MS605]: ...
    async def acquire_live(self) -> None: ...
    async def release_live(self) -> None: ...
```

- `MS605` 객체는 세션 하나에 하나이고 재연결해도 그대로 쓴다. push 핸들러는 생성자에서 한 번 등록하고
  `ms.on_disconnect`도 생성자에서 연결한다. 드라이버가 재연결 뒤에도 핸들러를 유지하므로 다시 등록하지 않는다.
- `connect(device)`: `device`를 주면 새 핸들로 바꾼다(CoreBluetooth는 버튼을 누른 뒤 새 핸들을 줄 수 있다).
  DISCONNECTED 또는 LOST에서만 부를 수 있다. CONNECTING/CONNECTED에서 부르면 `MS605Error`를 낸다.
- `reconnect_once()`: `scan(scan_secs)`를 한 번 돌려 이 주소가 광고 중이면 그 핸들로, 아니면 기존 핸들로
  `ms.reconnect()`를 한 번 시도한다. 실패하면 `MS605ConnectionError`를 그대로 올리고 상태는 이전 상태로 돌아간다.
- `reconnect(timeout)`: `reconnect_once()`를 2초 간격으로 `timeout`까지 반복한다. 끊긴 링크는 버튼을
  다시 눌러야만 돌아오므로, 이 함수는 "그 기기가 다시 광고하기를 기다리는" 일이다. 기한을 넘기면
  `MS605TimeoutError`, 취소되면 이전 상태(LOST 또는 DISCONNECTED)로 되돌리고 `CancelledError`를 다시 올린다.
  매 시도 전에 이미 CONNECTED면(다른 호출자나 수집 루프가 먼저 붙였으면) 바로 돌아오고, CONNECTING이면 시도하지 않고 기다린다.
  도중에 `close()`가 불리면 더 시도하지 않고 `MS605ConnectionError("session closed")`로 끝난다(앱이 놓은 세션을 되살리지 않는다).
- `close()`: DISCONNECTED로 가고, `RELEASE_TIMEOUT_S` 안에서 `ms.disconnect()`를 시도한 뒤(실패는 무시) keep-alive
  태스크가 끝나기를 기다린다. 끊기가 먼저다: 쓰기 중에 멈춘 ping은 링크가 끊겨야 끝나므로, 순서가 바뀌면 해제가
  ping 타임아웃(최대 10초)만큼 늦어진다. 여러 번 불러도 된다. 작업 잠금이 잡혀 있어도 부를 수 있고, 진행 중인 요청은 드라이버
  계약대로 `MS605ConnectionError`로 끝난다. CONNECTING 중에도 부를 수 있다. 그 연결은 끝난 뒤 링크를 바로 끊고
  `MS605ConnectionError("session closed while connecting")`로 끝나며, 상태는 DISCONNECTED로 남는다.
  드라이버를 건드리는 연결 시도는 한 번에 하나다. `close()` 뒤에 시작한 연결은 늦게 끝나는 이전 시도가 자기 링크를
  끊을 때까지 기다린다. 그래서 이전 시도의 정리가 새 링크를 끊지 않는다.
- `read_info()`: 작업 잠금(`"identify"`)을 잡고 tag30/23/21/36/53(53은 M3, 2.2절)을 `read_raw()` 한 번으로 읽는다. tag30이
  없거나 비어 있으면 `MS605Error`를 낸다. 성공하면 `device_id`와 `info`를 채운다.
- `acquire_live()` / `release_live()`: 실시간 출력(tag54)의 참조 카운트다. 여러 화면이 같은 센서를 볼 수 있기 때문에
  필요하다(D11). 4.3절을 보라.

### 4.2 링크 상태 머신

| 현재 | 트리거 | 다음 | 이벤트 |
|------|--------|------|--------|
| DISCONNECTED, LOST | `connect()`, `reconnect_once()` 시작 | CONNECTING | `LinkStateChanged` |
| CONNECTING | 연결 성공 | CONNECTED | `LinkStateChanged`. keep-alive 시작, `live`가 1 이상이면 tag54=1 다시 쓰기 |
| CONNECTING | 연결 실패 | 이전 상태(DISCONNECTED 또는 LOST) | `LinkStateChanged(reason=오류 문자열)` + `MS605ConnectionError` |
| CONNECTING | 취소 | 이전 상태 | `LinkStateChanged` + `CancelledError` |
| CONNECTED | 드라이버 `on_disconnect`(peer·유휴 끊김, 쓰기 정지로 링크 포기) | LOST | `LinkStateChanged(reason=...)`. keep-alive 중지 |
| CONNECTED | keep-alive가 링크 끊김으로 판단(4.4절) | LOST | 위와 같다 |
| CONNECTED, LOST, CONNECTING | `close()` | DISCONNECTED | `LinkStateChanged`(LOST → DISCONNECTED 포함). CONNECTING이면 그 연결은 위 4.1절대로 실패한다 |
| DISCONNECTED | `close()` | DISCONNECTED | 없음 |

LOST와 DISCONNECTED의 차이: LOST는 의도하지 않은 끊김이고, DISCONNECTED는 앱이 놓은 것이다. 둘 다 다시 붙으려면
버튼을 눌러야 한다. UI는 LOST에서만 "버튼을 다시 누르세요"를 띄운다.

### 4.3 작업 잠금과 `ms` 사용 규칙

```python
async with session.operation("apply") as ms:
    cfg = await ms.read_config()
    ...
```

- 잠금은 기기당 하나이고 **즉시 실패**한다. 잡혀 있으면 기다리지 않고 `SessionBusyError(reason=현재 이유)`를 낸다.
  여러 화면에서 같은 센서를 조작할 때, 보정 중인 센서에 대한 쓰기를 곧바로 거절하기 위해서다(D11).
- CONNECTED가 아니면 `MS605ConnectionError("not connected")`를 낸다. 자동 재연결은 하지 않는다. 재연결은
  사람의 버튼 누름이 필요하므로 호출자(CLI 프롬프트, GUI 화면)가 결정한다.
- 잡을 때 `BusyChanged(busy=reason)`, 놓을 때 `BusyChanged(busy=None)`을 보낸다. 예외나 취소로 빠져나가도 반드시 놓는다.
- 사용자 입력을 기다리는 동안 잠금을 잡고 있지 않는다. 기기 I/O 한 묶음만 감싼다.
- 쓰는 이유 문자열은 `"identify"`, `"read"`, `"apply"`, `"calibration"`이다. 상수로 두지 않고 문자열 그대로 쓴다.
- `ms`의 메서드는 `operation()` 안에서만 부른다. 예외는 세션 자신의 keep-alive ping과 tag54 쓰기뿐이다.
  이 둘은 잠금 없이 드라이버의 send lock으로만 직렬화된다. 그래서 보정이나 적용 중에도 링크가 유지되고,
  실시간 막대를 보면서 임계값을 쓸 수 있다.

`acquire_live()`는 카운트를 1 올리고, 0에서 1이 될 때 CONNECTED이며 `busy != "calibration"`이면 tag54=1을 쓴다.
쓰기가 실패하면 `MS605Error`를 올리지만 카운트는 올라간 채로 둔다. 그래서 호출자는 성공 여부와 관계없이
`finally`에서 `release_live()`를 부른다(기존 CLI 모니터의 try/finally 구조와 같다). `release_live()`는 카운트를
내리고, 1에서 0이 될 때 위 조건이면 tag54=0을 쓴다. 이때 오류는 로그만 남기고 올리지 않는다.
보정 중에는 기기가 스스로 tag55/56을 보내므로 tag54를 건드리지 않는다. 보정 중 tag54 쓰기의 효과는 측정되지 않았다.
그 대신 `"calibration"` 잠금을 놓을 때 CONNECTED이고 카운트가 보정 중에 0과 양수 사이를 넘나들었으면(잠금을 잡을 때의
`live > 0`과 지금이 다르면) tag54를 지금 카운트에 맞춰 한 번 쓴다. 오류는 로그만 남긴다. 그래서 보정 중에 연 화면도
보정이 끝나면 실시간 막대를 받고, 보정 중에 닫은 화면의 tag54=1은 꺼진다.

### 4.4 keep-alive

세션이 CONNECTED인 동안 `keepalive_interval`마다 `ms.ping()`을 보낸다. 판정 규칙은 M1 이전 CLI의 `_start_keepalive`와 같다.

| ping 결과 | 처리 |
|-----------|------|
| 성공 | 없음 |
| `MS605DeviceError` | 기기가 응답했으므로 링크는 살아 있다. `KeepAliveMissed(kind="error")` 후 계속 |
| `MS605TimeoutError`이고 `ms.is_connected` | ACK 유실 또는 CRC 불량. `KeepAliveMissed(kind="no_response")` 후 계속 |
| 그 밖의 예외 | LOST로 전이(`reason=str(exc)`), keep-alive 종료 |

ACK는 `min(WRITE_TIMEOUT_S, keepalive_interval)`까지 기다린다. 쓰기가 멈췄다고 보고 링크를 포기하는 기한은
간격과 관계없이 `WRITE_TIMEOUT_S`다(`ping(write_timeout=WRITE_TIMEOUT_S)`). 간격이 짧아도 느린 쓰기 하나로 링크를 버리지 않는다.

`operation(..., suspend_keepalive=True)`가 잡혀 있는 동안에는 ping을 건너뛴다. 이 옵션은 `CalibrationJob`만 쓴다.
`start_auto_calibration()`이 자기 keep-alive를 15초마다 보내므로, 세션 ping까지 겹치면 보정 중 트래픽이 두 배가 된다.
보정 중 keep-alive의 영향은 미측정이므로, 실기기에서 동작이 확인된 지금 방식(배치 보정은 발사 전에 수집용
keep-alive를 멈추고 드라이버 keep-alive 하나만 쓴다)을 유지한다.

잠금을 놓으면 즉시 ping 한 번을 보내고 주기를 다시 시작한다. 드라이버의 마지막 ping과 세션의 다음 ping 사이가
최대 30초까지 벌어져 29.6초 유휴 끊김에 걸리는 일을 막기 위해서다.

### 4.5 push → 이벤트

생성자에서 등록한 push 핸들러가 한 프레임의 모든 속성을 본다.

- tag55: `decode_radar_output()`. 성공하면 `last_radar`를 갱신하고 `LiveRadar`, `FrameError`이면 `FrameDropped(tag=55)`.
- tag56: `decode_pir_state()`. `last_pir`와 다르면(첫 값 포함) 갱신하고 `PirChanged`.
- 새 스트림이 시작되면 `last_pir`를 `None`으로 되돌린다. 그래서 그 스트림의 첫 tag56은 값이 같아도 `PirChanged`가 된다.
  새 스트림은 연결 시도 시작(이때 `last_radar`도 `None`), `acquire_live()`의 tag54=1 쓰기, 실시간 카운트가 0일 때의
  `"calibration"` 잠금이다. 이전 링크나 이전 스트림의 값이 현재 값처럼 보이지 않게 하기 위해서다.
- tag62와 그 밖의 tag는 무시한다. tag62는 드라이버의 push waiter가 `start_auto_calibration()`으로 전달한다.

## 5. `calibration.py`

### 5.1 공개 표면

```python
EXPECTED_CALIBRATION_S = 180.0
PREFLIGHT_WINDOW_S = 3.0

@dataclass(frozen=True)
class PresenceSnapshot:
    device_id: str | None
    samples: int              # 창 안에서 받은 tag55 수
    presence: bool | None     # 어느 표본이든 서브센서 재실이 하나라도 있으면 True. samples == 0이면 None
    pir: bool | None          # 창 안에서 PIR 감지가 한 번이라도 있었으면 True. 값을 못 봤으면 None
    occupied: bool | None     # presence 또는 pir가 True면 True, 둘 다 None이면 None, 그 밖에는 False
    error: str | None = None

async def presence_snapshot(session: DeviceSession, *, window_s: float = PREFLIGHT_WINDOW_S) -> PresenceSnapshot: ...

def build_calibration_record(
    device_name: str | None, address: str, cfg: MS605Config, *,
    device_id: str | None = None, when: datetime | None = None,
) -> dict: ...

class CalibrationJob:
    def __init__(
        self,
        session: DeviceSession,
        *,
        storage: Storage | None = None,
        timeout: float = CALIBRATION_TIMEOUT_S,
        expected_s: float = EXPECTED_CALIBRATION_S,
        progress_interval: float = 1.0,
        identify_wait: float = IDENTIFY_WAIT_S,      # (M3) 5.2절 첫 행
    ) -> None: ...
    session: DeviceSession
    state: CalibrationState          # 처음에는 IDLE
    result: CalibrationResult | None
    config_after: MS605Config | None  # SUCCEEDED 후 재조회한 설정(`result.after`의 원본). 재조회 실패면 None
    async def run(self) -> CalibrationResult: ...
    async def cancel(self) -> None: ...
```

**사전점검**(`presence_snapshot`): 세션이 CONNECTED가 아니면 `MS605ConnectionError`. `acquire_live()` 후
버스를 구독해 이 세션의 `LiveRadar`/`PirChanged`를 `window_s` 동안 모으고, `finally`에서 구독 해제와
`release_live()`를 한다. PIR은 `acquire_live()` 직후의 `last_pir`도 포함한다(이전 스트림의 값은 4.5절대로 지워져 있다). 판정에 PIR을 넣는 이유: GUI_PLAN 5장의
열린 질문(재실 판정 기준)에 대해 경고를 더 내는 쪽이 보수적이기 때문이다. 원값(`presence`, `pir`)을 함께 주므로
UI가 둘을 따로 보여 줄 수 있다. 사전점검은 경고일 뿐 보정을 막지 않는다(D6).

**보정 기록**: `build_calibration_record()`는 기존 함수를 옮긴 것이다. 기존 키(`timestamp`, `device_name`,
`device_address`, `sensitivity`, `detect_mode`, `zones`)를 같은 순서로 두고, 마지막에 `device_id`를 추가한다.
`device_id`가 없는 예전 줄도 그대로 유효하다(11.3절).

### 5.2 보정 상태 머신

| 현재 | 트리거 | 다음 | 비고 |
|------|--------|------|------|
| IDLE | `run()`, 잠금이 `"identify"` | (IDLE에서 대기) | (M3) 기기 I/O 없이 `identify_wait`초까지 50 ms 간격으로 `session.busy`를 다시 본다. 풀리면 아래 행들을 차례로 적용한다(잠금 확인과 획득 사이에 `await`가 없으므로 경쟁이 없다). 기한을 넘기면 다음 행대로 FAILED. 대기 중 `cancel()`이면 I/O 없이 CANCELLED(`started=False`). `"identify"`만 기다리는 이유: 사람이 시작한 작업이 아니라 세션이 스스로 잡는 tag 읽기 한 번이기 때문이다. 다른 이유(`"read"`, `"apply"`, `"calibration"`)는 지금처럼 즉시 실패한다(D11) |
| IDLE | `run()`, 잠금이 잡혀 있음 | FAILED | `error="busy: <이유>"`, `started=False` |
| IDLE | `run()`, CONNECTED가 아님 | LOST | `detail`=세션의 마지막 끊김 이유, `started=False` |
| IDLE | `run()`, 보정 전 tag51 읽기 실패 | LOST(`MS605ConnectionError`) / FAILED(그 밖) | `started=False` |
| IDLE | `run()`, 잠금 획득과 보정 전 읽기 성공 | STARTING | 잠금 `"calibration"`, `suspend_keepalive=True`. 그 뒤 tag52=4 쓰기 |
| IDLE | `cancel()` | CANCELLED | 기기 I/O 없음. 그 뒤 처음 부른 `run()`은 이 결과를 돌려준다(태스크를 만든 직후의 취소) |
| STARTING | `on_started` 콜백(ACK 수신) | LEARNING | 진행 타이머 시작 |
| STARTING, LEARNING | tag62=1 | SUCCEEDED | 보정 후 tag51 재조회, 이력 저장(아래) |
| STARTING, LEARNING | tag62≠1 | FAILED | `error=None`, `detail="device reported failure"` |
| STARTING, LEARNING | `MS605ConnectionError` | LOST | 세션도 LOST가 된다. 학습은 초기화된다(GUI_PLAN 1장) |
| STARTING, LEARNING | `MS605TimeoutError` | TIMEOUT | ACK가 오지 않은 경우(STARTING)도 같다. 기기가 학습을 시작했는지 알 수 없다 |
| STARTING, LEARNING | 그 밖의 `MS605Error` 또는 예상 밖 예외 | FAILED | `error=str(exc)`(`MS605DeviceError`는 tag52 쓰기 거절). `MS605Error`가 아닌 예외는 `"<타입 이름>: <메시지>"`(기존 CLI 출력과 같다) |
| STARTING, LEARNING | `cancel()` 또는 `run()` 태스크 취소 | CANCELLED | 5.3절 |

- 전이마다 `CalibrationStateChanged`를 보낸다. 종료 상태에 들어가면 `CalibrationResult`를 한 번 보내고
  `result`에 저장한 뒤 잠금을 놓는다.
- LEARNING 동안 `progress_interval`마다 `CalibrationProgress(elapsed_s, expected_s)`를 보낸다. `elapsed_s`는
  LEARNING에 들어간 뒤의 단조 시계 경과다. 비율 계산과 상한(예: 99%)은 UI가 정한다.
- `before`는 STARTING 직전에 읽은 tag51이다. SUCCEEDED이면 같은 잠금 안에서 `read_config()`로 `after`를 채운다.
  실패하면 `after=None`, `detail="반영값 재조회 실패: ..."`이고 상태는 SUCCEEDED로 둔다(M1 이전 CLI의 `_batch_save_result`와 같다).
  `storage`가 있으면 `build_calibration_record(session.name, session.address, cfg, device_id=...)`를
  `Storage.append_history()`로 저장하고 `history_saved=True`. `StorageError`이면 `detail="결과 저장 실패: ..."`.
  FAILED에서는 재조회와 저장을 하지 않는다.
- `start_auto_calibration(timeout=self.timeout, keepalive_interval=session.keepalive_interval, on_started=...)`로 부른다.
- `run()`은 기기 쪽 결과를 예외로 올리지 않고 모두 상태로 바꾼다. 배치에서 기기별로 격리하기 위해서다.
  예외가 되는 경우는 두 가지다. 두 번째로 부르면 `RuntimeError`, 태스크가 취소되면 `CancelledError`(5.3절 정리 후).

### 5.3 취소의 의미 (보수적 선택)

보정 중 링크를 유지한 채 기다리기를 그만두면 기기가 학습을 계속하는지 멈추는지는 **측정되지 않았다**.
선택지와 판단:

1. 링크를 유지하고 기다리기만 그만둔다: 기기가 나중에 임계값을 바꿀 수 있는데, 앱은 "취소됨"으로 보여 준다. 거부.
2. tag52를 이전 모드로 다시 쓴다: 실기기 효과가 미측정이고, 시뮬레이터는 학습 중 tag52=1~3 쓰기를 거절한다. 거부.
3. **링크를 끊는다**: GUI_PLAN 1장에 "도중에 링크가 끊기면 학습이 초기화된다"가 하드웨어 제약으로 적혀 있다.
   결과를 예측할 수 있는 유일한 방법이다. **채택.**

그래서 STARTING/LEARNING에서의 `cancel()`과 `run()` 태스크의 외부 취소는 같은 일을 한다. 대기를 취소하고
`session.close()`로 링크를 끊은 뒤, CANCELLED와 `detail="링크를 끊어 학습을 중단함. 다시 연결하려면 버튼을 누르세요"`를
남긴다. 세션은 DISCONNECTED가 된다. `cancel()`은 `run()`이 정리를 마칠 때까지 기다린 뒤 돌아온다.
외부 취소는 정리 후 `CancelledError`를 다시 올린다. UI는 다시 연결한 뒤 tag51을 읽어, 보정 전 값(`before`)과 같은지
확인하라고 안내한다. 이 동작은 M5 실기기 검증 항목으로 넘긴다.

## 6. `fleet.py`

### 6.1 수집과 연결

```python
class Fleet:
    def __init__(
        self,
        registry: Registry,
        storage: Storage,
        *,
        bus: EventBus | None = None,
        scan: Callable[[float], Awaitable[list[BLEDevice]]] | None = None,   # 기본 MS605.scan
        client_factory: Callable[..., object] | None = None,
        scan_secs: float = 5.0,
        connect_timeout: float = 10.0,
        keepalive_interval: float = KEEPALIVE_INTERVAL_S,
        gather_pause: float = GATHER_PAUSE_S,
    ) -> None: ...

    bus: EventBus
    registry: Registry
    storage: Storage
    sessions: Mapping[str, DeviceSession]     # device_id → 세션 (읽기 전용 뷰)
    gathering: bool
    connecting: int

    async def scan(self) -> list[BLEDevice]: ...
    async def connect(self, device: BLEDevice) -> DeviceSession: ...
    def start_gather(self, *, accept: Callable[[BLEDevice], bool] | None = None) -> None: ...
    async def stop_gather(self, *, finish_pending: bool = False) -> None: ...
    async def release(self, device_ids: Iterable[str] | None = None) -> None: ...
    async def aclose(self) -> None: ...
    async def __aenter__(self) -> Fleet: ...
    async def __aexit__(self, *exc: object) -> None: ...   # aclose()
```

- `scan()`: 스캔 한 번. CLI의 "목록에서 고르기" 수집이 쓴다.
- `connect(device)`: `DeviceSession`을 만들어 `connect()` → `read_info()` → `registry.match(...)`. 식별이 끝나면
  다른 `await` 없이 바로 `sessions[device_id]`에 넣고(그 뒤로는 `release()`가 이 세션을 맡는다), `SensorGathered`를 보낸 뒤
  세션을 돌려준다. 식별이 실패하면 세션을 닫고 예외를 그대로 올린다. `registry.match()`의 `StorageError`는 경고 로그만
  남기고 링크를 유지한다(주소 캐시·`last_seen` 갱신일 뿐이다). 이때 `SensorGathered.known`은 메모리의 레지스트리로 정한다.
  같은 `device_id`의 세션이 이미 있을 때:
  - 기존 세션이 CONNECTED/CONNECTING이면 새 링크를 닫고 `MS605Error("duplicate device id")`를 낸다
    (tag30 고유성은 1대에서만 확인됐기 때문에 이 경우를 정의해 둔다).
  - 기존 세션이 LOST/DISCONNECTED이면 새 세션으로 바꾼 뒤 기존 세션을 닫는다(주소가 바뀐 경우).
- `start_gather(accept)`: 백그라운드 루프를 시작한다. 이미 돌고 있으면 아무것도 하지 않는다. 루프는
  `scan(scan_secs)` → 장치마다 아래 규칙 → `gather_pause` 쉬기를 반복한다. `accept`가 있으면 `False`인 장치는
  무시한다(CLI `--address` 수집에 쓴다).
  - 주소가 CONNECTED/CONNECTING 세션이거나, 그 주소의 연결 태스크가 진행 중이면 건너뛴다.
  - 주소가 `release()`가 닫고 있는 세션이면 건너뛴다.
  - 주소가 LOST/DISCONNECTED 세션이면 그 세션의 `connect(device)`를 태스크로 돌린다. 버튼을 다시 누른 센서가
    이렇게 돌아온다. 세션 객체를 그대로 쓰므로 실시간 카운트와 기존 참조가 유지된다. 연결되면 `connect()`처럼
    tag30을 다시 읽고, `device_id`가 같으면 `registry.match()` 후 `SensorGathered`를 보낸다. 다르면 세션의 식별 정보는
    그대로 두고 링크를 닫은 뒤 `MS605Error("device id changed ...")`로 실패한다(`GatherFailed`). tag30 읽기 자체가
    실패하면(예: CONNECTED를 보고 다른 호출자가 먼저 잠금을 잡아 `SessionBusyError`) 링크는 닫지 않고 `GatherFailed`만
    보낸다. 식별은 처음 수집할 때 이미 확인됐다.
  - 그 밖에는 `self.connect(device)`를 태스크로 돌린다.
  - 태스크가 실패하면 `GatherFailed`를 보낸다. 그 주소는 다음 스캔에서 다시 시도된다(지금 `--collect`와 같다).
- `stop_gather(*, finish_pending: bool = False)`: 스캔 루프를 취소하고, 진행 중인 연결 태스크를 모두 취소하고 기다린다.
  `finish_pending=True`이면 진행 중인 연결 태스크가 끝나기를 먼저 기다린다(CLI `--collect`에서 Enter 직전에 버튼을 누른
  센서도 배치에 들어가게 한다). 이미 연결된 세션은 그대로 둔다. 돌아온 뒤에는 어떤 링크도 새로 생기지 않는다.
- `connecting: int`(읽기 전용): 수집 루프가 진행 중인 연결 시도 수. CLI의 "마무리 대기" 줄이 쓴다.
- `release(ids)`: 첫 `await` 전에 세션들을 "닫는 중"으로 표시해 수집 루프가 다시 붙이지 않게 하고, 해당 세션의
  진행 중 연결 태스크를 먼저 취소하고, 세션들을 동시에 `close()`한 뒤 `sessions`에서 뺀다.
  `None`이면 전부이고, 아직 식별 중이라 `sessions`에 없는 `connect()`의 세션도 닫는다(그 `connect()`는
  `MS605ConnectionError`로 끝난다). 작업 세션형(D5)이므로 놓은 센서는 레지스트리의 마지막 값으로만 보인다.
- `aclose()`: 끝나지 않은 배치(`calibrate()`로 만든 것)를 모두 `cancel()`하고, `stop_gather()` 후 `release()`. 예외가 나도 끝까지 정리한다.

### 6.2 일괄 보정

```python
class BatchCalibration:
    batch_id: str                         # uuid4().hex
    device_ids: tuple[str, ...]
    fire_at: float                        # epoch 초
    state: BatchState
    jobs: dict[str, CalibrationJob]       # 발사 시점에 만든다. 그 전에는 비어 있다
    results: dict[str, CalibrationResult]
    async def wait(self) -> dict[str, CalibrationResult]: ...
    async def cancel(self) -> None: ...
    def retry_ids(self) -> list[str]: ...  # 결과가 FAILED, LOST, TIMEOUT인 id

class Fleet:
    async def preflight(self, device_ids: Sequence[str], *, window_s: float = PREFLIGHT_WINDOW_S) -> dict[str, PresenceSnapshot]: ...
    def calibrate(self, device_ids: Sequence[str], *, start: float | datetime = 0.0, timeout: float = CALIBRATION_TIMEOUT_S) -> BatchCalibration: ...
```

- `preflight()`: 기기마다 `presence_snapshot()`을 동시에 돌린다. 한 기기의 오류는 그 기기의
  `PresenceSnapshot(samples=0, error=...)`가 된다.
- `calibrate()`: 동기 함수다. 배치를 만들고 실행 태스크를 띄운 뒤 바로 돌려준다(GUI가 즉시 응답해야 하기 때문).
  `start`는 지연 초(0이면 즉시, N이면 N초 뒤) 또는 `datetime`(시각 예약, naive면 로컬 시각)이다. 지난 시각이면
  `ValueError`, 빈 목록이면 `ValueError`, `sessions`에 없는 id면 `KeyError`다. "HH:MM"을 해석해 다음 날로 넘기는 일은
  CLI의 `resolve_target_datetime`이 맡는다.
- 배치 상태 머신:

| 현재 | 트리거 | 다음 | 동작 |
|------|--------|------|------|
| (생성) | `calibrate()` | WAITING | `BatchChanged`. 기기 잠금은 잡지 않는다. 세션의 keep-alive가 링크를 유지한다 |
| WAITING | `fire_at` 도달 | RUNNING | 1초 이하 단위로 `time.time()`을 다시 확인하며 기다린다(시계 변경과 노트북 잠자기에 강하다) |
| WAITING | `cancel()` | CANCELLED | 기기 I/O 없음. 각 id에 `CalibrationResult(CANCELLED, started=False)` |
| RUNNING | 모든 작업 종료 | DONE | |
| RUNNING | `cancel()` | CANCELLED | 모든 작업에 `cancel()`을 동시에 부른다(링크가 끊긴다, 5.3절) |

- 발사: 각 id에 대해 그 시점의 `sessions[id]`로 `CalibrationJob(session, storage=self.storage, timeout=timeout)`을
  만들고 모두 `asyncio.gather(..., return_exceptions=True)`로 동시에 돌린다. 세션이 없거나 배치를 만들 때의 세션 객체가
  아니면(그 사이 `release()`되거나 다시 수집돼 바뀐 경우. 나중에 다른 일로 모은 센서를 예약 배치가 보정하지 않게 한다)
  작업을 만들지 않고
  `CalibrationResult(LOST, started=False, detail="세션 없음")`을 보낸다. 대기 중에 끊긴 센서는 작업의 IDLE → LOST로
  기록된다. 한 기기에서 무슨 일이 나도 다른 기기의 작업은 멈추지 않는다.
- 끊긴 센서만 재시도: 사람이 해당 센서의 버튼을 누르면 수집 루프(또는 `session.reconnect()`)가 링크를 되살리고,
  그 뒤 `fleet.calibrate(batch.retry_ids())`로 새 배치를 만든다. 재시도 전용 API는 따로 두지 않는다.
- 배치는 여러 개가 동시에 있을 수 있다. 같은 기기가 두 배치에 들어가면 먼저 발사한 쪽이 잠금을 잡고,
  나중 쪽의 작업은 `FAILED(error="busy: calibration")`이 된다.
- 전후 비교(D6)는 `CalibrationResult.before`/`after`로 한다.

### 6.3 초안 (Draft)

```python
@dataclass
class ThresholdChange:
    relative: bool                  # True: 현재값 + 값, False: 값으로 덮어쓰기
    trigger: list[int | None]       # 존 7개. None이면 그 존은 그대로
    maintain: list[int | None]

@dataclass
class SensorChanges:
    sensitivity: int | None = None
    detect_mode: int | None = None
    zone_enable: list[bool] | None = None
    zone_thresholds: ThresholdChange | None = None
    subsensor_zones: list[list[int]] | None = None
    subsensor_timing: list[tuple[int, int]] | None = None
    subsensor_enable: list[bool] | None = None

    def validate(self) -> None: ...                                # 현재값 없이 할 수 있는 검사. ProfileError
    def resolve(self, current: ConfigProfile) -> ConfigProfile: ...  # 바꿀 섹션만 담은 목표 프로파일
    @classmethod
    def from_profile(cls, profile: ConfigProfile, sections: Sequence[str]) -> SensorChanges: ...

@dataclass
class Draft:
    targets: list[str]                                  # device_id, 적용 순서
    bulk: SensorChanges = field(default_factory=SensorChanges)
    per_sensor: dict[str, SensorChanges] = field(default_factory=dict)
    def changes_for(self, device_id: str) -> SensorChanges: ...
    def validate(self) -> None: ...
```

- 섹션 이름은 `PROFILE_SECTION_KEYS`와 같다. `zone_thresholds`만 `ThresholdChange`로 감싸고 나머지는
  `ConfigProfile`의 같은 이름 필드와 같은 모양이다.
- `changes_for(id)`: `bulk`에서 시작해 `per_sensor[id]`에 값이 있는 섹션을 **섹션 단위로 통째로** 바꾼다(깊은 병합은 하지 않는다).
- `validate()`(현재값 불필요): 대상이 비어 있지 않을 것, `ThresholdChange`의 두 목록이 길이 7이고 항목이 `int` 또는
  `None`(`bool` 제외)일 것, 절대값은 0..65535일 것, 나머지 섹션은 `ConfigProfile.validate()` 규칙. 어기면 `ProfileError`.
- `resolve(current)`: 상대값은 존마다 `current + delta`, 절대값은 그 값, `None`은 현재값이다. 결과가 0..65535를
  벗어나면 `ProfileError`다. **잘라 내지 않는다.** 조용히 다른 값을 쓰는 것보다 그 센서를 실패로 보이는 편이 안전하다.
  결과 프로파일을 `ConfigProfile.validate()`로 한 번 더 검사한다. `subsensor_timing`의 쌍은 `from_config()`처럼 튜플로 맞춘다
  (JSON에서 온 목록 쌍도 쓰기 후 검증에서 같게 비교되도록). 상대값이 기본이라는 정책(D8)은 UI가 정한다. 코어는 두 방식을 모두 받는다.
- `from_profile(profile, sections)`: 클론용이다. 임계값은 `ThresholdChange(relative=False, ...)`가 된다.
  `detect_mode == 4`도 그대로 담는다. 실제로 쓸 때 `apply_profile()`이 건너뛰고, 결과의 `skipped`에 남는다(지금 클론과 같다).

### 6.4 적용, 검증, 되돌리기, 클론

```python
async def poll_verify(
    ms: MS605, target: ConfigProfile, sections: Sequence[str], *,
    timeout: float = VERIFY_TIMEOUT_S, interval: float = VERIFY_INTERVAL_S,
) -> tuple[ConfigProfile, list[str]]: ...     # (재조회한 프로파일, 아직 다른 섹션). 기존 confirm_profile_applied를 옮긴 것

async def apply_changes(
    session: DeviceSession, changes: SensorChanges, storage: Storage, *,
    reason: str = "apply", verify_timeout: float = VERIFY_TIMEOUT_S,
) -> ApplyResult: ...

class Fleet:
    async def apply(self, draft: Draft) -> dict[str, ApplyResult]: ...
    async def rollback(self, device_id: str, snapshot: str) -> ApplyResult: ...
```

`apply_changes()`는 기기 하나의 파이프라인이다. 단일 센서 CLI 흐름과 `Fleet.apply`가 같이 쓴다.
`session.operation("apply")` 안에서 차례로 진행한다.

| 단계 | 동작 | 실패하면 |
|------|------|----------|
| 1. 현재값 | `read_config()` → `ConfigProfile.from_config()` | FAILED, 쓴 것 없음 |
| 2. 검증 | `target = changes.resolve(current)`, `sections = target.sections_present()` | FAILED(`error`=ProfileError), 쓴 것 없음 |
| 3. 스냅샷 | `storage.save_snapshot(device_id, current, sections, reason)` | FAILED(`error`=StorageError), 쓴 것 없음. 되돌릴 지점이 없으면 쓰지 않는다 |
| 4. 쓰기 | `applied = await ms.apply_profile(target, sections)`. `skipped = sections - applied` | FAILED(`MS605Error`가 아닌 예외는 `error="<타입 이름>: <메시지>"`). 링크가 살아 있으면 한 번 재조회해 `mismatched`를 채운다(SPEC 6.2: 무엇이 써졌는지 가정하지 않는다). 재조회도 실패하면 비워 둔다. `snapshot`은 남기므로 되돌릴 수 있다 |
| 5. 검증 | `poll_verify(ms, target, applied)`. 측정된 반영 지연(약 1초) 때문에 최대 3초 폴링한다 | 재조회 자체가 실패하면 UNVERIFIED |
| 결과 | `mismatched`가 비면 OK, 아니면 PARTIAL | |

- 세션이 식별되지 않았으면(`device_id is None`, `read_info()` 전) I/O 없이 FAILED(`error="device not identified: ..."`)다.
  스냅샷을 기기 ID별로 남기기 때문이다.
- 잠금 획득이 실패하면(`SessionBusyError`, 미연결) 결과는 FAILED이고 `error`에 이유를 쓴다. 이 함수도 기기 쪽
  결과를 예외로 올리지 않는다. `CancelledError`만 올린다.
- 결과 `ApplyResult`를 세션 버스로 보내고 돌려준다. `snapshot`은 저장된 스냅샷 이름(11.2절)이다.
- `Fleet.apply(draft)`: 먼저 `draft.validate()`를 한다. 여기서 `ProfileError`가 나면 어떤 기기에도 연결하거나 쓰기 전에
  올린다. 대상이 `sessions`에 없으면 `KeyError`다. 그 뒤 `targets` 순서대로 **하나씩** `apply_changes()`를 부른다.
  CONNECTED가 아닌 대상은 I/O 없이 FAILED(`error="not connected"`)다. 순차로 하는 이유: 지금 클론과 같은 출력 순서를
  지키고, 동시 쓰기의 BLE 부하가 측정되지 않았기 때문이다. 7대 × (쓰기 + 최대 3초 검증) 정도면 순차로도 충분하다.
  차례를 기다리는 대상은 세션 keep-alive로 연결을 유지한다.
- `rollback(device_id, snapshot)`: `storage.load_snapshot()`으로 읽고
  `apply_changes(session, SensorChanges.from_profile(snap.profile, snap.sections), storage, reason="rollback")`를
  부른다. 되돌리기도 적용 전에 스냅샷을 남기므로 되돌리기를 다시 되돌릴 수 있다. 스냅샷이 없거나 깨졌으면 `StorageError`.
- **클론**: 원본의 `ConfigProfile`(기기에서 읽거나 파일에서 불러온 것)로
  `Draft(targets, bulk=SensorChanges.from_profile(profile, sections))`를 만들어 `Fleet.apply()`에 넘긴다. 클론 전용 코드는 없다.
- 차이 미리보기(D7)는 코어 API를 따로 두지 않는다. 호출자가 `changes.resolve(current)`와
  `ConfigProfile.diff_sections()`로 만든다.

## 7. `registry.py`

```python
@dataclass
class Site:
    site_id: str          # 사용자가 정한 짧은 slug. 예: "lab-a"
    name: str

@dataclass
class Sensor:
    device_id: str        # tag30 소문자 hex, 영구 키(D4)
    site_id: str
    alias: str
    location: str = ""
    notes: str = ""
    addresses: dict[str, str] = field(default_factory=dict)   # 호스트 → BLE 주소 캐시
    last_seen: str | None = None                              # ISO 8601 UTC
    battery_pct: int | None = None

@dataclass
class PendingSensor:
    site_id: str
    alias: str
    address: str
    host: str
    source: str           # 가져온 파일 이름

@dataclass(frozen=True)
class MatchResult:
    sensor: Sensor | None
    resolved_pending: bool

class Registry:
    def __init__(self, storage: Storage, *, host: str | None = None) -> None: ...   # host 기본 platform.node()
    host: str
    sites: Mapping[str, Site]
    sensors: Mapping[str, Sensor]
    pending: Sequence[PendingSensor]

    def add_site(self, site_id: str, name: str) -> Site: ...
    def add_sensor(self, device_id: str, site_id: str, alias: str, *, location: str = "", notes: str = "") -> Sensor: ...
    def update_sensor(self, device_id: str, *, site_id: str | None = None, alias: str | None = None,
                      location: str | None = None, notes: str | None = None) -> Sensor: ...
    def remove_sensor(self, device_id: str) -> None: ...
    def match(self, device_id: str, address: str, *, battery_pct: int | None = None) -> MatchResult: ...
    def address_for(self, device_id: str) -> str | None: ...
    def import_sensor_info(self, path: Path, site_id: str, *, site_name: str | None = None) -> list[PendingSensor]: ...
```

- 생성할 때 `storage.read_json(storage.registry_path)`로 읽는다. 파일이 없으면 빈 레지스트리이고, 형식이 틀리면 `StorageError`다.
  **변경 메서드는 매번 바로 원자적으로 저장한다.** 파일이 작고, 저장 시점을 호출자가 신경 쓰지 않게 하기 위해서다.
- 오류: 없는 사이트나 센서는 `KeyError`, 이미 있는 사이트나 센서를 추가하면 `ValueError`.
- `match(device_id, address)`: `Fleet.connect()`가 식별 직후 부른다. 주소는 대소문자를 무시하고 비교한다.
  1. 등록된 센서면 `addresses[host] = address`, `last_seen`, `battery_pct`를 갱신하고 저장한다.
  2. 아니고, 이 호스트의 대기 항목 중 주소가 같은 것이 있으면 그 항목으로 센서를 만들고(`site_id`, `alias`) 대기 목록에서
     지운 뒤 저장한다. `resolved_pending=True`.
  3. 아니면 `MatchResult(None, False)`이고 아무것도 저장하지 않는다. 새 센서 등록은 UI가 `add_sensor()`로 한다
     (M2 "센서 모으기"). 코어가 마음대로 등록하지 않는다.
- 호스트 식별은 `platform.node()`다. 주소 캐시는 캐시일 뿐이어서, 호스트 이름이 바뀌어 캐시를 못 찾아도 연결 후 tag30으로
  다시 맞춰진다. 잃는 것이 없으므로 가장 단순한 방법을 쓴다.
- `import_sensor_info(path, site_id)`: 기존 `sensor_info_*.yaml`(한 줄에 `이름: 주소`)을 YAML 의존성 없이 읽는다.
  - 줄 양끝 공백을 지운다. 빈 줄과 `#`으로 시작하는 줄은 건너뛴다.
  - 첫 번째 `": "`(콜론+공백)에서 나눈다. Linux MAC 주소의 콜론은 공백이 뒤따르지 않으므로 그대로 남는다.
    값을 감싼 따옴표(`'`, `"`) 한 쌍은 벗긴다.
  - 나눌 수 없거나 이름 또는 주소가 비면 `StorageError("<파일>:<줄번호>: ...")`이고 아무것도 추가하지 않는다(전부 아니면 전무).
  - 사이트가 없으면 `site_name`(없으면 `site_id`)으로 만든다. 항목은 `host=self.host`인 대기 항목이 된다.
    파일의 주소는 그 파일을 만든 노트북의 주소이므로, 가져오는 호스트가 그 노트북이라고 가정한다.
  - 이미 같은 (사이트, 주소, 호스트)의 대기 항목이 있거나, 이 호스트에서 그 주소로 캐시된 센서가 있으면 건너뛴다.
    그래서 같은 파일을 다시 가져와도 된다. 새로 추가한 항목만 돌려준다.
  - 파일의 사이트 이름(파일명)은 실제 장소일 수 있으므로 저장소와 테스트에는 합성 파일(`sensor_info_example.yaml`)만 쓴다.

## 8. `storage.py`

```python
def data_root() -> Path: ...    # cli.resolve_data_dir()를 그대로 옮긴 것

@dataclass(frozen=True)
class Snapshot:
    name: str              # 파일 이름에서 .json을 뺀 것, 예: "20261001T120000123456Z"
    device_id: str
    taken_at: str          # ISO 8601 UTC
    reason: str            # "apply" | "rollback"
    sections: tuple[str, ...]
    profile: ConfigProfile

class Storage:
    def __init__(self, root: Path | None = None) -> None: ...   # 기본 data_root() / "cal_results"
    root: Path
    registry_path: Path       # root / "registry.json"
    history_path: Path        # root / "calibration_history.jsonl"
    snapshots_dir: Path       # root / "snapshots"

    def read_json(self, path: Path) -> dict | None: ...
    def write_json_atomic(self, path: Path, data: dict) -> None: ...
    def append_history(self, record: dict) -> Path: ...
    def read_history(self, device_id: str | None = None, *, addresses: Iterable[str] = ()) -> list[dict]: ...
    def save_snapshot(self, device_id: str, profile: ConfigProfile, sections: Sequence[str], reason: str) -> Snapshot: ...
    def list_snapshots(self, device_id: str) -> list[Snapshot]: ...      # 최신이 앞
    def load_snapshot(self, device_id: str, name: str) -> Snapshot: ...
```

- `data_root()`의 우선순위는 지금과 같다: `$MS605_DATA_DIR` → 소스 체크아웃이면 저장소 루트 → `platformdirs.user_data_dir("ms605", appauthor=False)`.
  `cli.py`의 `resolve_data_dir`와 `_REPO_ROOT`는 지운다.
- **모든 파일은 `data_root()/cal_results/` 아래에 둔다.** 이유는 두 가지다. (1) 기존 `calibration_history.jsonl` 경로가
  바뀌지 않는다. (2) 소스 체크아웃에서 실행하면 데이터가 저장소 루트에 생기는데, `cal_results/`는 `.gitignore`에 있으므로
  실제 Device ID와 주소가 담긴 `registry.json`이 공개 저장소에 커밋될 위험이 없다.
- `read_json`: 없으면 `None`. 읽기 실패(`OSError`)나 JSON 오류는 `StorageError(...) from exc`.
- `write_json_atomic`: 같은 디렉터리에 `tempfile.NamedTemporaryFile(delete=False)`로 쓰고, flush와 `os.fsync` 후
  `os.replace`한다. 부모 디렉터리는 만든다. UTF-8, `ensure_ascii=False`, `indent=2`, 마지막 줄바꿈. 실패하면 임시 파일을
  지우고 `StorageError`. 직렬화 실패(`json.dumps`의 `TypeError`/`ValueError`, 짝 없는 서로게이트의 `UnicodeEncodeError`)도
  `StorageError`다. 프로세스 하나만 쓴다고 가정한다(CLI와 GUI를 동시에 띄우면 마지막에 쓴 쪽이 이긴다, 16장).
- `append_history`: M1 이전 CLI의 `save_calibration_record`와 같다(한 줄 JSON 추가). `OSError`는 `StorageError`로 감싼다.
- `read_history(device_id, addresses=...)`: 깨진 줄은 경고 로그만 남기고 건너뛴다(추가 전용 파일의 마지막 줄이 잘려도
  나머지는 읽혀야 한다). `device_id`를 주면 그 `device_id`인 줄과, `device_id`가 없는 예전 줄 중
  `device_address`가 `addresses`(그 센서의 캐시된 주소들)에 있는 줄을 돌려준다.
- 스냅샷 경로는 `snapshots/<device_id>/<UTC %Y%m%dT%H%M%S%fZ>.json`이다. 콜론이 없어 어느 OS에서도 쓸 수 있고 이름순이 시간순이다.
  M1에서는 지우지 않는다(보존 정책은 16장).

## 9. 오류 계약

코어가 올리는 예외는 `MS605Error` 하위 클래스, 호출자 실수를 뜻하는 `KeyError`/`ValueError`/`RuntimeError`,
그리고 `CancelledError`뿐이다. bleak 예외는 드라이버가 이미 `MS605ConnectionError`로 감싼다.

| 상황 | 예외 |
|------|------|
| `operation()` 시 잠금이 잡혀 있음 | `SessionBusyError(reason)` |
| `operation()`, `read_info()`, `presence_snapshot()` 시 CONNECTED가 아님 | `MS605ConnectionError` |
| 연결 실패, 진행 중 링크 끊김 | `MS605ConnectionError`(드라이버) |
| 응답 없음, `reconnect(timeout)` 기한 초과 | `MS605TimeoutError` |
| 기기가 0이 아닌 status를 줌 | `MS605DeviceError` |
| CONNECTING/CONNECTED에서 `connect()`, tag30 없음, 같은 `device_id`의 중복 링크 | `MS605Error` |
| 초안이나 프로파일 값이 틀림 | `ProfileError`(`ValueError`이기도 하다) |
| `registry.json`, 스냅샷, 가져오기 파일, 이력을 읽거나 쓰지 못함 | `StorageError` |
| 없는 `device_id`, `site_id`, `sessions`에 없는 대상 | `KeyError` |
| 중복 추가, 빈 대상 목록, 지난 예약 시각 | `ValueError` |
| `CalibrationJob.run()`을 두 번 부름 | `RuntimeError` |

**기기별로 격리하는 API는 예외 대신 결과를 돌려준다.** `CalibrationJob.run()`, `BatchCalibration.wait()`,
`apply_changes()`, `Fleet.apply()`(사전 `validate()` 제외), `Fleet.rollback()`(스냅샷 읽기 제외),
`Fleet.preflight()`가 그렇다. 기기 하나의 실패가 나머지를 멈추지 않게 하기 위해서다(M0 HIGH #2).

## 10. 동시성 규칙

- 이벤트 루프 하나, 스레드 없음. 코어 객체는 만든 루프에서만 쓴다.
- **기기당 작업 잠금 하나**(4.3절), 즉시 실패. 잠금을 잡는 쪽:

| 잡는 쪽 | 이유 | keep-alive | 범위 |
|---------|------|------------|------|
| `DeviceSession.read_info()` | `"identify"` | 계속 | tag 읽기 한 번 |
| CLI의 읽기 흐름 | `"read"` | 계속 | 읽기 한 묶음 |
| `apply_changes()` | `"apply"` | 계속 | 현재값 → 스냅샷 → 쓰기 → 검증 |
| `CalibrationJob` | `"calibration"` | 멈춤(드라이버 keep-alive가 대신) | STARTING부터 종료 상태까지 |

- 잠금 없이 동시에 해도 되는 것: keep-alive ping, `acquire_live`/`release_live`의 tag54 쓰기, push 처리, `close()`.
  드라이버의 send lock이 프레임 조각이 섞이지 않게 막는다.
- 기기 사이: 연결, 식별, 사전점검, 보정은 기기 수만큼 동시에 한다. 초안 적용만 순차다(6.4절).
- 수집 루프는 Fleet당 하나다. 수집과 배치 보정은 동시에 돌 수 있다. 보정 중 스캔의 영향은 측정되지 않았으므로
  CLI는 지금처럼 발사 전에 수집을 끝낸다. GUI는 배치가 RUNNING이 되면 모으기를 멈춘다(`docs/GUI_API.md` 14.1절 G20).
- (M3) `CalibrationJob.run()`은 `"identify"` 잠금만 `identify_wait`(기본 2초)까지 기다린다(5.2절). 그 밖의 잠금 규칙은 위와 같다.
- 동시 링크 수는 제한하지 않는다(2대까지만 확인). 한도를 넘으면 연결 실패가 `GatherFailed`로 보인다. 한도 측정은 M5로 넘긴다.
- 취소: 모든 공개 코루틴은 취소되면 잠금을 놓고, 만든 태스크를 정리하고, `CancelledError`를 다시 올린다.
  보정 중 취소만 링크를 끊는다(5.3절).

## 11. 영속 형식

모든 예시 값은 합성이다.

### 11.1 `registry.json`

```json
{
  "format": "ms605-registry",
  "version": 1,
  "sites": {
    "lab-a": {"name": "Lab A"}
  },
  "sensors": {
    "0000000000000000000000000000000000000001": {
      "site_id": "lab-a",
      "alias": "Sensor 1",
      "location": "north wall",
      "notes": "",
      "addresses": {"host-1": "00000000-0000-4000-8000-000000000001"},
      "last_seen": "2026-10-01T12:00:00+00:00",
      "battery_pct": 87
    }
  },
  "pending": [
    {"site_id": "lab-a", "alias": "Sensor 2", "address": "00000000-0000-4000-8000-000000000002",
     "host": "host-1", "source": "sensor_info_example.yaml"}
  ]
}
```

센서는 사이트 아래에 중첩하지 않고 `device_id`로 평평하게 둔다. 한 센서는 사이트 하나에 속하고, ID가 전역에서
유일하다는 것이 구조로 보장된다. `format`이나 `version`이 다르면 `StorageError`다.

### 11.2 스냅샷 `snapshots/<device_id>/<name>.json`

```json
{
  "format": "ms605-snapshot",
  "version": 1,
  "device_id": "0000000000000000000000000000000000000001",
  "taken_at": "2026-10-01T12:00:00.123456+00:00",
  "reason": "apply",
  "sections": ["zone_thresholds"],
  "profile": {"format": "ms605-config-profile", "version": 1, "source_name": null, "source_address": null,
              "sections": {"sensitivity": 4, "zone_thresholds": [[70, 30], [62, 30], [55, 28], [48, 26], [42, 24], [36, 22], [30, 20]]}}
}
```

`profile`은 기존 `ConfigProfile.to_dict()` 형식이고 쓰기 전 **전체** 설정이다. `sections`는 그 적용이 쓰려던 섹션이며,
되돌리기는 이 섹션만 쓴다. 위 예시의 `profile.sections`는 줄였다. 실제로는 7개 섹션이 모두 들어간다.

### 11.3 `calibration_history.jsonl`

기존 형식을 그대로 두고 `device_id` 키만 마지막에 추가한다. 한 줄에 하나:

```json
{"timestamp": "2026-10-01T12:00:00+00:00", "device_name": "MRBL_SIM01", "device_address": "00000000-0000-4000-8000-000000000001", "sensitivity": 4, "detect_mode": 2, "zones": [{"index": 0, "distance_m": 0.8, "trigger": 70, "maintain": 30}], "device_id": "0000000000000000000000000000000000000001"}
```

(`zones`는 실제로 7개다.) `device_id`가 없는 예전 줄도 유효하다.

### 11.4 프로파일 파일

`ms605 clone --save/--from-file`의 파일 형식(`ms605-config-profile`)은 바꾸지 않는다. 사용자가 고른 경로에 쓰는
파일이므로 `Storage`가 아니라 CLI가 계속 다룬다.

## 12. CLI 이전

M1 완료 기준은 "기존 CLI 명령의 동작과 출력이 같다"이다. CLI는 출력과 입력만 맡고, 기기 다루기는 모두 코어를 부른다.

### 12.1 옮기거나 바꾸는 것

| 지금 (`ms605/cli/`) | M1 이후 |
|---------------------|---------|
| `resolve_data_dir`, `_REPO_ROOT`, `CALIBRATION_HISTORY_PATH` | `storage.data_root()`, `Storage().history_path`. 이력 경로를 바꾸던 테스트는 `MS605_DATA_DIR` 또는 `Storage(root=tmp_path)`를 쓴다 |
| `build_calibration_record`, `save_calibration_record` | `calibration.build_calibration_record`, `Storage.append_history` |
| `FALLBACK_DISTANCES_M`, `zone_distances` | `models.py` |
| `LiveLink` (`_shared.py`) | `DeviceSession`. `LiveLink.ensure()`의 안내 문구와 무한 재시도는 CLI 도우미 `ensure(session)`로 남긴다. 이 도우미는 경고를 출력하고 `session.reconnect_once()`를 2초 간격으로 반복한다 |
| `connect_with_retry` | 같은 출력, 내부는 `session.connect(device)` 반복 |
| `ManagedDevice`, `_start_keepalive`, `_cancel_keepalives`, `_stop_keepalives` | 삭제. 세션이 keep-alive를 맡는다. 기존 로그 줄은 버스 구독으로 출력한다(`LinkStateChanged(LOST)` → "연결 끊김 (발사 전 대기 중)", `KeepAliveMissed` → `kind`별로 "keep-alive 응답 오류 (연결 유지)" / "keep-alive 응답 없음 (연결 유지, 재시도)") |
| `_batch_gather_collect` | `fleet.start_gather()` + Enter + `fleet.stop_gather(finish_pending=True)`(기존 "진행 중인 연결 시도 N건 마무리 대기" 줄 유지). 중단 경로는 `fleet.stop_gather()`. `SensorGathered`/`GatherFailed`를 기존 문구로 출력 |
| `_batch_gather_menu` | `fleet.scan()` → 체크박스 → 고른 장치마다 `fleet.connect(dev)` |
| `_batch_gather_address` | `fleet.scan()`에서 주소나 이름이 맞는 장치를 찾아 `fleet.connect()`. 없으면 지금처럼 다시 검색할지 묻는다 |
| `_batch_await_fire_trigger` | Enter를 기다린 뒤 `fleet.calibrate(ids, start=0.0)` |
| `_batch_hold` | `fleet.calibrate(ids, start=resolve_target_datetime(...))`. 대기 중 heartbeat 출력은 CLI가 `fleet.sessions`의 상태로 만든다 |
| `_batch_fire`, `_batch_save_result` | `BatchCalibration.wait()` + `CalibrationResult` 출력 |
| `_batch_release` | `fleet.aclose()` |
| `confirm_profile_applied` | `fleet.poll_verify` |
| `_clone_apply_all` | `fleet.apply(Draft(targets, bulk=SensorChanges.from_profile(profile, sections)))` |
| `confirm_zone_thresholds`와 단일 센서 쓰기 흐름(`flow_set_zone`, `flow_detailed_adjustment`, `flow_set_sensitivity`, `flow_zone_enable`, `flow_subsensor_zones`, `flow_subsensor_timing`) | `apply_changes(session, SensorChanges(...), storage)`. 출력 문구는 같고, 이제 스냅샷이 남고 쓰기 후 검증이 폴링된다. 스냅샷을 저장하지 못하면(데이터 디렉터리에 쓸 수 없음) 6.4절 3단계대로 쓰지 않고 "작업 실패"로 끝난다(M1 이전에는 디스크를 쓰지 않았다) |
| `flow_auto_calibration` | `CalibrationJob(session, storage=..., progress_interval=5.0)`. 실시간 줄은 `LiveRadar`/`PirChanged`, "경과 Ns"는 `CalibrationProgress`, 결과 줄은 `CalibrationResult`로 출력. 반영값 표는 다시 읽지 않고 `job.config_after`로 그린다. `_await_calibration_trigger`의 자체 keep-alive는 지운다(세션이 유지한다) |
| `flow_live_monitor`, `_flow_live_monitor_plain` | 버스 구독 + `acquire_live()`/`release_live()` |
| `read_device_info` | `session.read_info()`. 지금처럼 `MS605Error`를 삼키고 "?"를 출력한다 |
| 그 밖의 읽기 흐름(`read-dnd`, `read-pir`, `read-history`, `sync-time` …) | `async with session.operation("read") as ms:` 안에서 기존 드라이버 호출 |

### 12.2 CLI에 남는 것

argparse와 `main()`, 종료 코드, 모든 렌더링(`format_*`, `render_monitor`, `_ui`), 모든 프롬프트(`ainput`,
체크박스, 확인), 장치 선택 메뉴(`discover_and_select`), 버튼 안내 문구, `resolve_target_datetime`("HH:MM" 해석),
`resolve_sections`(`--only/--skip`), `merge_thresholds`(미리보기용), `save_profile`/`load_profile`, `--log-file` 출력.
`ms605/cli/ble.py`(`ms605-driver`)는 드라이버를 직접 쓰는 저수준 도구이므로 손대지 않는다.
`calibrate`/`clone`은 `Registry`를 만들다 `StorageError`가 나면(깨진 `registry.json`) "레지스트리 파일 오류: ..."를
출력하고 종료 코드 2로 끝난다.

### 12.3 요약 표의 상태 문자열

요약 출력이 같도록 CLI는 코어 결과를 기존 문자열로 바꾼다.

| CLI 상태 | 코어 |
|----------|------|
| `connected` | 결과 없음(발사나 적용 전에 중단) |
| `lost` | `CalibrationResult(LOST, started=False)`, 또는 결과 없이 중단됐을 때 세션이 LOST(이유는 마지막 `LinkStateChanged(LOST).reason`) |
| `calibrated_ok` | `SUCCEEDED` |
| `calibrated_fail` | `FAILED`, `error is None` |
| `calibration_timeout` | `TIMEOUT` |
| `calibration_lost` | `LOST`, `started=True` |
| `calibration_error` | `FAILED`, `error is not None` |
| `clone_ok` / `clone_partial` / `clone_unverified` / `clone_error` | `ApplyStatus.OK` / `PARTIAL` / `UNVERIFIED` / `FAILED` |

`CANCELLED`는 지금 CLI에 없는 상태다. CLI는 Ctrl-C에서 배치를 취소하지 않고, 지금처럼 `aclose()`로 모두 해제한다.

## 13. M2에서의 노출 (참고)

M2 서버는 이 표면을 감싸기만 한다.

- 명령은 REST: `Fleet.start_gather/stop_gather/release/preflight/calibrate/apply/rollback`, `BatchCalibration.cancel`,
  `Registry`의 변경 메서드, `import_sensor_info`.
- 상태는 `/ws`: 클라이언트마다 `bus.stream()` 하나를 열고 `{"type": 클래스 이름, **asdict(ev)}`를 보낸다. 접속할 때와
  `dropped`가 늘었을 때는 `fleet.sessions`(상태·`busy`·`last_radar`·`last_pir`·`info`)와 레지스트리를 통째로 다시 보낸다.
- 초안은 화면마다 클라이언트가 갖고(D11), 적용할 때만 `Draft`를 보낸다.
- 오류 매핑: `SessionBusyError` → 409, `KeyError` → 404, `ProfileError`/`ValueError` → 422,
  `MS605ConnectionError` → 409(연결 필요), `StorageError` → 500.

## 14. 테스트

모든 동작은 `ms605/sim.py`로 검증한다. 건너뛰기, xfail, 빈 테스트는 두지 않는다. 전체 스위트는 60초보다 충분히 짧아야 한다.

- 주입: `fleet = SimFleet(n, speed=...)`이면 `Fleet(..., scan=fleet.discover, client_factory=fleet.client_factory)`,
  `DeviceSession(dev.ble_device, bus, scan=fleet.discover, client_factory=fleet.client_factory)`. `SimFleet.discover`는
  `return_adv=False`일 때 장치 목록을 돌려주므로 `scan` 시그니처에 맞는다. 몽키패치는 필요 없다.
- 시간 비율: 시뮬레이터의 시간은 기기 초 ÷ `speed`이고 코어의 시간 인자는 벽시계 초다. `speed=100`이면 유휴 끊김이
  0.3초이므로, 테스트는 `keepalive_interval=15/speed`, 보정 `timeout=200/speed`처럼 줄여서 넘긴다. 버튼 창(120초)과
  `apply_delay`(1초)도 같은 비율로 줄어든다.
- 저장소: `Storage(root=tmp_path)`. 실제 사용자 데이터 디렉터리를 건드리지 않는다.
- 드라이버의 청크 간격(`INTER_CHUNK_DELAY_S`, 벽시계 20ms)은 `speed`로 줄지 않는다. 코어가 만드는 `MS605`에는 이 인자가
  없으므로, 테스트는 `tests/conftest.py`의 `no_chunk_pacing` 픽스처로 기본값을 0으로 바꾼다.

| 파일 | 꼭 검증할 것 |
|------|--------------|
| `tests/test_events.py` | 구독 순서, 해제 함수, 디스패치 중 해제, 콜백 예외 → `HandlerFailed` 후 다음 구독자 실행, `HandlerFailed` 처리 중 예외는 재귀하지 않음, 스트림 drop-oldest와 `dropped`, `close()` 후 `StopAsyncIteration`, `asdict`→`json.dumps` 가능 |
| `tests/test_session.py` | 4.2절 표의 전이 각각, 유휴 끊김 없이 keep-alive로 유지, `drop_link()` → LOST, `KeepAliveMissed`(`inject_status`/`drop_responses`), 잠금 즉시 실패와 `BusyChanged`, 미연결 시 `operation()` 거절, `reconnect()`이 버튼을 다시 누를 때까지 기다림·기한 초과·취소, live 참조 카운트와 재연결 후 tag54 다시 쓰기, 짧은 tag55 → `FrameDropped`, `PirChanged`는 바뀔 때만 |
| `tests/test_calibration.py` | 성공(`before`/`after`, 이력 한 줄과 `device_id`), `calibration_result=0` → FAILED, 보정 중 `drop_link()` → LOST, 결과 없음 → TIMEOUT, tag52 status 오류 → FAILED, 잠금 충돌 → FAILED(`busy`), 미연결 → LOST(`started=False`), LEARNING 중 `cancel()` → 링크가 끊기고 기기 tag51이 그대로, 외부 태스크 취소도 같음, 진행 이벤트, 보정 중 세션 ping이 멈추고 끝난 뒤 즉시 ping, 사전점검의 재실/부재/표본 없음. (M3) `"identify"` 대기: 0.3초 뒤 풀리면 SUCCEEDED, `identify_wait=0.2`인데 1초 잡혀 있으면 0.2초 이상 기다린 뒤 FAILED(`busy: identify`), 대기 중 `cancel()` → CANCELLED이고 tag52 쓰기 없음, `"read"` 잠금은 기다리지 않고 즉시 FAILED |
| `tests/test_session.py` (M3) | `read_info()`의 `info.zone_distances_m`이 시뮬레이터 tag53과 같음(`zone_distances(info) == (0.8, 1.6, 2.4, 3.2, 4.0, 4.8, 5.6)`), tag53을 지운 기기는 `None`이고 `zone_distances(info) == FALLBACK_DISTANCES_M` |
| `tests/test_fleet.py` (M3) | **재수집 직후 발사 회귀**: `SimFleet(1, speed=100)`, 수집 중인 센서에 `dev.response_delay = 10.0`(벽시계 0.1초: identify 읽기가 그만큼 잠금을 잡는다)을 주고 `drop_link()` → `press_button()`. 재수집이 잡는 `BusyChanged(busy="identify")`를 버스 콜백이 보는 즉시 `fleet.calibrate([id])` → 결과 SUCCEEDED(수정 전에는 `FAILED("busy: identify")`). `CONNECTED`가 아니라 잠금 이벤트에 거는 이유: 그래야 발사가 잠금 구간 안에 확실히 들어간다 |
| `tests/test_fleet.py` | 수집이 광고 중인 장치만 연결, 알려진/새 센서 이벤트, 대기 항목 해결, 연결 실패 → `GatherFailed` 후 다음 스캔에서 재시도, LOST 세션이 버튼을 누르면 같은 세션으로 복귀, `stop_gather()` 뒤 새 링크 없음, 중복 ID, 배치 즉시/N초/시각, 대기 중 취소, 7대 중 1대를 보정 중 끊어도 6대 성공(M3 기준을 코어에서 먼저), `retry_ids()` 재배치, 상대·절대 초안과 범위 초과 → 그 센서만 FAILED, `per_sensor` 섹션 덮어쓰기, `apply_delay` 아래에서 폴링 검증 OK, `inject_status` → FAILED + 스냅샷, `rollback()`으로 복원, 클론 |
| `tests/test_registry.py` | 사이트·센서 CRUD와 오류, 변경마다 저장, `import_sensor_info`의 주석·빈 줄·따옴표·MAC 주소·잘못된 줄(줄 번호, 전부 아니면 전무)·다시 가져오기, 호스트별 주소 캐시 |
| `tests/test_storage.py` | `MS605_DATA_DIR` 우선순위, 원자적 쓰기(실패 시 기존 파일 유지, 임시 파일 정리), 깨진 JSON → `StorageError`, 이력 깨진 줄 건너뛰기와 예전 줄 주소 매칭, 스냅샷 이름 순서와 왕복 |
| `tests/test_core_e2e.py` | 시뮬레이터로 수집 → 보정 → 클론 종단(M1 완료 기준) |
| 기존 `tests/test_cli_*.py` | CLI 출력이 그대로인지. 내부 함수를 몽키패치하던 테스트는 코어 경계에 맞게 고친다 |

코어에 출력이 없는지는 정적 검사로 확인한다: 코어 모듈 소스에 `print(`, `rich`, `questionary`, `ms605.cli`가 없어야 한다.

## 15. 구현 순서

서로 겹치지 않게 나눌 수 있는 순서다. 각 단계는 테스트와 ruff가 통과한 상태로 끝낸다.

1. `errors.py`·`models.py`·`driver.py`의 작은 변경(2.1절), `storage.py`, `events.py` (서로 독립)
2. `registry.py`(storage만 필요), `session.py`(events만 필요)
3. `calibration.py`
4. `fleet.py`
5. CLI 이전(12장)과 종단 테스트

## 16. 범위 밖 (M2+)

- FastAPI 서버, `/ws`, `--lan` 토큰과 QR, pydantic 스키마와 TS 타입 생성, `ms605 gui --sim N` (M2)
- 실시간 모니터·보정·편집 화면, 임계선 드래그, 상대값 기본 정책의 UI (M3, M4)
- 스냅샷 보존 정책(오래된 스냅샷 지우기), 여러 단계 실행 취소
- 레지스트리 내보내기·가져오기(yaml 가져오기 제외), 여러 노트북 사이의 레지스트리 공유와 병합
- 여러 프로세스가 같은 데이터 디렉터리를 쓸 때의 파일 잠금
- 동시 연결 수 제한과 대기열, 초안의 병렬 적용
- 예약 보정 중 잠자기 방지(macOS `caffeinate`)
- 보정 취소 동작, 보정 중 keep-alive 영향, 실제 보정 시간, 동시 연결 한도의 실기기 검증 (M5)
- 이력(tag58/60)·DND·시간 동기화의 GUI, 다국어
- 데스크톱 패키징(Tauri, PyInstaller)
