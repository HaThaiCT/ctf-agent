# CTF Agent — hướng dẫn sử dụng

**Đối tượng:** người đã quen thao tác terminal, Docker và có quyền tham gia một cuộc thi CTF dùng CTFd. Chỉ chạy công cụ với challenge, dịch vụ và tài khoản mà bạn được phép sử dụng.

CTF Agent điều phối nhiều AI solver để giải challenge CTF. Ở chế độ đầy đủ, một **coordinator** theo dõi CTFd và khởi chạy **swarm** cho từng challenge; mỗi solver có một container Docker riêng. Có thể chạy một challenge trước với `--no-submit` để kiểm tra cấu hình mà không gửi flag lên CTFd. Lưu ý: chế độ này **vẫn có thể gọi model, phát sinh chi phí và kết nối tới dịch vụ của challenge**.

Theo thông tin của dự án, công cụ đã giải 52/52 challenge và đạt hạng nhất tại BSidesSF 2026 CTF. Dự án được xây dựng bởi [Veria Labs](https://verialabs.com), do thành viên của đội [.;,;.](https://ctftime.org/team/222911) (smiley) sáng lập.

## Mục lục

- [Cách hệ thống hoạt động](#cách-hệ-thống-hoạt-động)
- [Yêu cầu trước khi cài](#yêu-cầu-trước-khi-cài)
- [Cài đặt và chuẩn bị](#cài-đặt-và-chuẩn-bị)
- [Cách 1: giải một challenge](#cách-1-giải-một-challenge)
- [Cách 2: chạy coordinator với CTFd](#cách-2-chạy-coordinator-với-ctfd)
- [Cấu hình 9router cho coordinator và solver](#cấu-hình-9router-cho-coordinator-và-solver)
- [Các tùy chọn CLI và model](#các-tùy-chọn-cli-và-model)
- [Theo dõi và khắc phục lỗi](#theo-dõi-và-khắc-phục-lỗi)
- [Giới hạn và lưu ý an toàn](#giới-hạn-và-lưu-ý-an-toàn)

## Cách hệ thống hoạt động

```text
CTFd ──> bộ thăm dò (mỗi 5 giây) ──> coordinator (Claude SDK hoặc Codex)
                                         │
                                         ├── swarm / challenge A
                                         │     ├── solver cấu hình 1 ──> Docker riêng
                                         │     ├── solver cấu hình 2 ──> Docker riêng
                                         │     └── solver cấu hình 3 ──> Docker riêng
                                         └── swarm / challenge B ──> ...
```

- Coordinator là **AI agent**, không phải chỉ là logic Python cố định. Backend vẫn đảm nhiệm thăm dò CTFd, tải challenge, quản lý vòng đời swarm, container, trace và việc gửi flag. `--coordinator claude` dùng Claude Agent SDK; `--coordinator codex` dùng Codex app-server.
- Một swarm chạy một solver cho mỗi **cấu hình** `--models` đã chọn; có thể dùng hai mức effort của cùng một model để có hai solver riêng. Các solver cùng challenge chia sẻ phát hiện qua `ChallengeMessageBus` và các lượt thử tiếp theo có thể dùng thông tin từ solver khác. Với **một** cấu hình, sẽ không có phát hiện từ solver khác để trao đổi.
- Dữ liệu challenge nằm trong `metadata.yml` và thư mục `distfiles/`. Container gắn metadata và distfiles dạng chỉ đọc tại `/challenge/`, còn `/challenge/workspace/` dành cho file làm việc. Claude SDK solver dùng Claude Code/Agent SDK làm agent harness; hook chuyển lệnh Bash vào container. Các backend solver khác gồm Codex và Pydantic AI cho một số provider API.
- Khi gửi flag thật, backend xử lý chống gửi trùng và thời gian chờ sau những lần gửi sai. Khi CTFd xác nhận `correct`/`already_solved` — kể cả do thành viên cùng đội nộp ngoài công cụ — backend hủy các solver của challenge, đợi đóng tiến trình agent và xóa container/workspace rồi mới nhả slot cho challenge đang chờ. Poller phát hiện lần nộp ngoài trong tối đa khoảng một chu kỳ 5 giây. Nếu Docker từ chối xóa container, slot được giữ lại và lỗi được ghi log thay vì cấp quá tài nguyên. `--no-submit` tắt việc gửi flag lên CTFd; kết quả model trong chế độ này **không phải xác nhận từ CTFd**.

## Yêu cầu trước khi cài

1. **Python 3.14+**, [`uv`](https://docs.astral.sh/uv/) và Docker đang chạy, với quyền sử dụng Docker trên máy cục bộ. Cần đủ dung lượng cho image công cụ CTF và tài nguyên cho các container; mức giới hạn bộ nhớ mặc định trong cấu hình backend là `16g` mỗi container (có thể đổi bằng `CONTAINER_MEMORY_LIMIT`).
2. Tài khoản/token CTFd hợp lệ cho cuộc thi được phép dùng. Với chế độ một challenge và `--no-submit`, bạn có thể chuẩn bị challenge cục bộ mà không cần gửi flag; chế độ coordinator vẫn truy cập CTFd để thăm dò và tải dữ liệu.
3. CLI và thông tin đăng nhập cho backend bạn chọn: **Claude Code CLI / Claude Agent SDK** khi dùng `claude-sdk/*` hoặc coordinator Claude; **Codex CLI** khi dùng `codex/*` hoặc coordinator Codex. Gói Python khai báo phụ thuộc `claude-agent-sdk`; hãy kiểm tra `claude --version` và đăng nhập Claude CLI theo phương thức bạn sử dụng. Với provider API khác (Bedrock/Azure/Zen/Google), cần cấu hình credential/provider tương ứng.
4. Nếu dùng 9router, cần router cục bộ tương thích **Anthropic Messages**, model ID mà router hỗ trợ và credential **mới**. Khi Claude Code CLI trên máy đã trỏ vào 9router như phiên hiện tại, cả coordinator và Claude SDK solver có thể kế thừa cấu hình ấy; không cần thêm `SOLVER_ROUTER_*` vào `.env`.

> Docker sandbox **không** phải ranh giới an toàn tuyệt đối: cấu hình container có quyền bổ sung, tắt seccomp và có thể truy cập mạng. Không chạy dữ liệu không tin cậy trên Docker host chứa tài sản quan trọng. Chỉ kết nối đến mục tiêu trong phạm vi cho phép của CTF.

## Cài đặt và chuẩn bị

Chạy các lệnh sau **tại thư mục gốc repository**:

```bash
uv sync
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .
cp .env.example .env
```

Kiểm tra Docker bằng `docker info`; nếu daemon chưa chạy hoặc tài khoản không có quyền truy cập Docker, hãy xử lý trước khi khởi chạy solver. `ctf-solve` mặc định dùng image `ctf-sandbox`; nếu bạn build bằng tên khác, truyền `--image <tên-image>`.

Sửa `.env` bằng trình biên tập của bạn. Khi đã cấu hình Claude Code CLI trên máy dùng 9router cho **cả coordinator và solver**, chỉ cần điền CTFd tại máy của bạn:

```dotenv
CTFD_URL=https://ctf.example.com
CTFD_TOKEN=<nhập-token-trên-máy-của-bạn>
SOLVER_MODEL=claude-sdk/ag/claude-sonnet-4-6
```

`SOLVER_MODEL` là lựa chọn mặc định **một** solver khi không truyền `--models`; lệnh ba solver phía dưới sẽ ghi đè nó. Để `SOLVER_ROUTER_*` không được thiết lập nhằm kế thừa gateway Claude Code sẵn có cho cả hai vai trò. `.env` được gitignore nhưng vẫn là file nhạy cảm: không đưa credential vào tham số CLI, log chia sẻ hoặc commit. Các biến provider trong `.env.example` được comment và để trống; chỉ bỏ comment biến thực sự dùng. Nếu từng gửi key trong tin nhắn, hãy thu hồi/xoay key đó trước khi sử dụng.

Nếu **không dùng router**, bỏ các dòng `SOLVER_ROUTER_*` và `SOLVER_MODEL`, sau đó chọn model bằng `--models` hoặc để ứng dụng dùng danh sách mặc định. Danh sách mặc định cần cả Claude và Codex CLI/credential tương ứng; cách dễ kiểm tra nhất là chỉ định **một** model đã cấu hình.

## Cách 1: giải một challenge

Tạo challenge cục bộ, ví dụ `challenges/demo/metadata.yml`:

```yaml
name: Demo
category: misc
value: 100
description: 'Mô tả challenge được phép giải.'
connection_info: ''
```

Nếu có file đính kèm, đặt trong `challenges/demo/distfiles/`. `connection_info` có thể là URL hoặc lệnh kết nối của dịch vụ challenge; solver sẽ dùng thông tin này để truy cập mục tiêu. Có thể dùng dữ liệu do chế độ coordinator tự tải về hoặc script `pull_challenges.py` bên dưới thay vì tạo bằng tay.

Chạy thử **không gửi flag**:

```bash
uv run ctf-solve --challenge challenges/demo --no-submit --models claude-sdk/claude-opus-4-6 -v
```

Nếu đã đặt `SOLVER_MODEL` trong `.env`, bỏ `--models` để dùng model đó, ví dụ:

```bash
uv run ctf-solve --challenge challenges/demo --no-submit -v
```

Chế độ `--challenge` **chạy swarm trực tiếp, không chạy coordinator**; cờ `--coordinator` không ảnh hưởng tới chế độ này. Ứng dụng kiểm tra `metadata.yml`, khởi động các solver đã chọn và in kết quả cùng thống kê sử dụng. Nếu không có file metadata, ứng dụng báo lỗi. Khi chạy live (không có `--no-submit`) với token hoặc tài khoản CTFd đã cấu hình, chế độ này cũng thăm dò trạng thái đội: bài đã giải từ đầu sẽ không khởi chạy solver, còn solve xuất hiện trong lúc chạy sẽ hủy swarm và đợi cleanup. `--no-submit` không ngăn solver chạy lệnh hoặc truy cập dịch vụ challenge; chỉ ngăn ứng dụng gửi flag đến CTFd và cũng không bật cơ chế theo dõi solve ngoài ở chế độ một challenge. Khi bỏ `--no-submit`, cần CTFd URL/credential hợp lệ và `name` trong metadata phải khớp tên challenge trên CTFd để gửi flag.

Nếu muốn tải challenge thủ công từ CTFd trước khi chạy một challenge, repository có `pull_challenges.py` (script **riêng**, không phải lệnh `ctf-solve`). Script cần `aiohttp` và `beautifulsoup4` ngoài các phụ thuộc đã khai báo trong `pyproject.toml`. Sau khi cài hai gói đó vào môi trường Python của bạn, xem tùy chọn bằng `python pull_challenges.py --help`; script hỗ trợ `--url`, `--output` và **một trong** `--token` hoặc `--username` kèm `--password`. Lưu ý: script có thể tự mở khóa các hint **miễn phí** trên CTFd; xác nhận điều này phù hợp với quy định cuộc thi trước khi chạy.

## Cách 2: chạy coordinator với CTFd

Đảm bảo `.env` đã có `CTFD_URL` và `CTFD_TOKEN` (hoặc cấu hình `CTFD_USER`/`CTFD_PASS`). Chạy khởi đầu với giới hạn một challenge đồng thời, một model và **không gửi flag**:

```bash
uv run ctf-solve \
  --coordinator claude \
  --models claude-sdk/claude-opus-4-6 \
  --challenges-dir challenges \
  --max-challenges 1 \
  --no-submit \
  -v
```

Nếu không truyền `--models`, CLI dùng một `SOLVER_MODEL` trong `.env` (nếu đã đặt). Với Claude Code CLI hiện trỏ vào 9router, coordinator Claude và Claude SDK solver kế thừa cùng gateway; ví dụ model mặc định `claude-opus-4-6` của coordinator **không** nằm trong hai ID router từng liệt kê, nên hãy chọn một ID router có sẵn bằng `--coordinator-model`. Nếu chọn `--coordinator codex`, hãy đăng nhập/cấu hình Codex CLI trước. Backend thăm dò challenge CTFd, tải dữ liệu về `--challenges-dir` và tự khởi chạy swarm cho challenge chưa giải. `--max-challenges` giới hạn số **challenge** đồng thời, không phải số solver: với nhiều cấu hình solver, số container có thể xấp xỉ `số challenge × số cấu hình solver`. Khi một swarm kết thúc, challenge chưa được khởi chạy sẽ được xếp vào slot trống; challenge vừa kết thúc không được tự chạy lại nếu chưa có yêu cầu retry từ coordinator. Dừng bằng `Ctrl+C`.

Cấu hình được yêu cầu: **ba solver trong mỗi challenge, tối đa hai challenge đồng thời** (có thể dùng tới **sáu container solver**, mỗi container mặc định giới hạn `16g` bộ nhớ, cộng thêm tiến trình coordinator):

```bash
uv run ctf-solve \
  --coordinator claude \
  --coordinator-model ag/claude-sonnet-4-6 \
  --models claude-sdk/ag/claude-sonnet-4-6/low \
  --models claude-sdk/ag/claude-sonnet-4-6/high \
  --models claude-sdk/ag/claude-opus-4-6-thinking/medium \
  --challenges-dir challenges \
  --max-challenges 2 \
  --no-submit \
  -v
```

`claude-sdk/` là tiền tố **nội bộ** để chọn agent harness, không nằm trong ID model gửi tới router. Hai hậu tố Sonnet `/low` và `/high` cùng hậu tố Opus `/medium` được truyền riêng qua Claude SDK tới CLI `--effort`; `-thinking` là một phần tên Opus trên router. Có thể đổi riêng từng solver sang `/low`, `/medium`, `/high`, `/max`, hoặc bỏ hậu tố để dùng mặc định SDK. Hai cấu hình Sonnet là hai **solver riêng biệt**, không phải model thứ ba trên 9router. SDK và tài liệu Anthropic hỗ trợ tham số effort cho hai dòng model 4.6, nhưng **chưa kiểm chứng** 9router hiện có chuyển tiếp và áp dụng tham số đó hay không. Việc liệt kê model và test offline không chứng minh tương thích suy luận/streaming/tool use; cần kiểm tra bằng một lần chạy có sự đồng ý về chi phí trước khi dùng thực tế. Không bỏ `--no-submit` nếu chưa xác nhận quyền nộp flag.

**Chỉ sau khi đã xác nhận cấu hình, mục tiêu và quyền gửi flag**, mới chạy lại mà không có `--no-submit`. Khi đó solver có thể tự gửi flag lên CTFd, gây ra các lần nộp không thể thu hồi.

Trong lúc coordinator đang chạy, log sẽ báo địa chỉ cổng nhận tin nhắn operator tại `127.0.0.1`. Để dùng cổng cố định và gửi một gợi ý từ terminal thứ hai:

```bash
uv run ctf-solve --coordinator claude --coordinator-model ag/claude-sonnet-4-6 --models claude-sdk/ag/claude-sonnet-4-6/medium --no-submit --msg-port 9400 --max-challenges 1
uv run ctf-msg --port 9400 'Hãy kiểm tra các challenge web trước.'
```

Dòng thứ nhất chạy liên tục ở terminal thứ nhất; dòng thứ hai chạy ở terminal thứ hai. Nếu muốn kết nối cổng trên máy khác, CLI `ctf-msg` có `--host`, nhưng server operator hiện chỉ lắng nghe `127.0.0.1`, nên mặc định chỉ dùng trên cùng máy.

## Cấu hình 9router cho coordinator và solver

**9router chỉ cung cấp endpoint LLM**: Claude Code CLI/Agent SDK vẫn vận hành agent, gọi tool và hook; backend vẫn quản lý Docker, CTFd, trace và message bus. Nếu Claude Code CLI đã cấu hình sẵn `ANTHROPIC_BASE_URL` và phương thức xác thực 9router, cả coordinator Claude và các Claude SDK solver kế thừa cấu hình đó. `.env` cục bộ đang chọn cách này: `SOLVER_ROUTER_*` được để trống. Kiểm tra gateway còn hoạt động trước khi chạy; phiên `claude --help` trong lúc chuẩn bị đã báo 9router không sẵn sàng và dùng danh sách model đã lưu.

1. Khởi động 9router cục bộ và kiểm tra các model được phép dùng. `ag/claude-sonnet-4-6` và `ag/claude-opus-4-6-thinking` từng xuất hiện trong danh sách không xác thực của router tại máy này; danh sách đó **không bảo đảm** bạn được phép suy luận hoặc các thao tác SDK hoạt động. `--coordinator-model` nhận **nguyên ID** của router; `--models` nhận `claude-sdk/<ID>[/effort]` để backend biết chọn Claude SDK solver.
2. Sau khi tự điền URL/token CTFd vào `.env` và có quyền Docker, chạy một challenge đã được phép với `--challenge ... --models claude-sdk/ag/claude-sonnet-4-6/medium --no-submit` trước. Kiểm tra SDK gọi đúng gateway và trace/tool call vẫn chạy trong Docker, rồi mới thử lệnh ba solver trong phần trên. Cả hai bước đều có thể phát sinh chi phí và truy cập mạng dù không gửi flag.
3. Nếu **muốn dùng gateway riêng chỉ cho solver** thay vì cho cả hai vai trò, đặt `SOLVER_ROUTER_URL=http://127.0.0.1:20128/v1`, `SOLVER_ROUTER_KEY=<key-mới>` và `SOLVER_ROUTER_AUTH=bearer` (hoặc `x-api-key`) trong `.env`, cùng `SOLVER_MODEL=claude-sdk/<router-model-id>`. Khi ấy `SOLVER_ROUTER_*` chỉ ghi đè môi trường của solver; coordinator dùng cấu hình Claude Code hiện có, vốn cần được đặt độc lập nếu không muốn đi cùng router. Ứng dụng chuyển URL có `/v1` về URL gốc vì Claude Code tự thêm `/v1/messages`. Không đưa key vào lệnh hoặc tài liệu.

Chưa kiểm chứng request có xác thực qua 9router, streaming/tool use, hoặc việc router tôn trọng `--effort` trên các ID `ag/...` bằng lần chạy thực tế. Router cần hỗ trợ Anthropic Messages và truyền đúng các trường, header, sự kiện streaming Claude Code yêu cầu. Trường hợp endpoint chỉ trả lời text theo giao thức khác, cấu hình URL sẽ không đủ để chạy Claude SDK.

## Các tùy chọn CLI và model

Xem danh sách đầy đủ trong `uv run ctf-solve --help` và `uv run ctf-msg --help`. Các tùy chọn chính:

| Tùy chọn | Tác dụng |
| --- | --- |
| `--challenge DIR` | Giải một thư mục challenge, bỏ qua coordinator. |
| `--challenges-dir DIR` | Thư mục lưu challenge khi chạy coordinator; mặc định `challenges`. |
| `--ctfd-url URL`, `--ctfd-token TOKEN` | Ghi đè URL/token trong `.env`; tránh token trên dòng lệnh nếu không muốn xuất hiện trong lịch sử shell/process list. |
| `--models SPEC` | Lặp lại để chọn nhiều solver, ví dụ `--models claude-sdk/claude-opus-4-6 --models codex/gpt-5.4`; ghi đè `SOLVER_MODEL`. |
| `--coordinator claude\|codex` | Backend coordinator, mặc định `claude`; chỉ dùng trong chế độ đầy đủ. |
| `--coordinator-model ID` | Model coordinator, mặc định là `claude-opus-4-6` với coordinator Claude. |
| `--max-challenges N` | Số challenge đồng thời tối đa, mặc định `10`. |
| `--no-submit` | Không gửi flag lên CTFd; model request và tool/network vẫn hoạt động. |
| `--image NAME` | Docker image solver; mặc định `ctf-sandbox`. |
| `--msg-port PORT` | Cổng thông điệp operator; `0` để chọn tự động. |
| `-v` | Bật log chi tiết. |

Nếu **không** truyền `--models` và **không** đặt `SOLVER_MODEL`, ứng dụng dùng lineup mặc định trong `backend/models.py`: `claude-sdk/claude-opus-4-6/medium`, `claude-sdk/claude-opus-4-6/max`, `codex/gpt-5.4`, `codex/gpt-5.4-mini`, `codex/gpt-5.3-codex`. Mỗi model có thể tạo một container và phát sinh chi phí/giới hạn tài khoản riêng. Khi router bật, chỉ chấp nhận solver có tiền tố `claude-sdk/`; có thể truyền nhiều lần `--models` để chạy nhiều solver Claude SDK sau khi đã kiểm tra một model hoạt động.

## Theo dõi và khắc phục lỗi

- **Trace:** mỗi solver ghi sự kiện JSONL dưới `logs/trace-*.jsonl` (bao gồm lời gọi tool và kết quả rút gọn). Có thể theo dõi bằng `tail -f logs/trace-...jsonl`. Trace có thể chứa nội dung challenge và flag; không chia sẻ nguyên bản nếu chưa rà soát.
- **Không tìm thấy `metadata.yml`:** kiểm tra đường dẫn sau `--challenge`; file bắt buộc phải nằm trực tiếp trong thư mục challenge.
- **Docker không chạy/không tạo container:** thử `docker info`, xác nhận image `ctf-sandbox` đã build và Docker host cho phép cấu hình container trong `backend/sandbox.py` (quyền, thiết bị `/dev/loop-control`, tài nguyên bộ nhớ). Khi khởi động, ứng dụng cố dọn các container cũ mang nhãn `ctf-agent`; tránh chạy nhiều phiên công cụ độc lập trên cùng Docker host.
- **Coordinator không đăng nhập hoặc request đi nhầm router:** kiểm tra cấu hình Claude Code mà coordinator kế thừa. Khi chọn 9router cho cả hai vai trò, đặt `--coordinator-model` thành ID đang có trên router; `SOLVER_ROUTER_*` nếu được bật sẽ chỉ ghi đè cấu hình của solver.
- **Router báo 401/400 hoặc streaming lỗi:** xác minh key mới, lựa chọn `bearer`/`x-api-key`, model ID và khả năng hỗ trợ Anthropic Messages (gồm tool use và streaming). Việc endpoint `/v1/models` trả lời không chứng minh suy luận Claude Code hoạt động.
- **Không nhận tin nhắn operator:** kiểm tra port được in trong log hoặc dùng `--msg-port` cố định; server chỉ lắng nghe localhost.
- **Thống kê chi phí:** đây là dữ liệu ứng dụng ghi nhận; với gateway/router hoặc gói subscription, hãy đối chiếu hạn mức/thanh toán ở provider thực tế.

Để kiểm tra mã nguồn khi phát triển:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

Các kiểm tra toàn repository có thể báo lỗi trong phần mã chưa sửa; đừng nhầm việc chạy được solver với việc mọi kiểm tra chất lượng đều đạt. `pull_challenges.py` có hai phụ thuộc riêng như đã nêu ở trên.

## Giới hạn và lưu ý an toàn

- `--no-submit` **không phải chế độ offline**: solver vẫn có thể liên hệ model, URL dịch vụ challenge hoặc các đích mà công cụ của solver gọi. Chỉ cấp cho agent môi trường và quyền truy cập phù hợp phạm vi CTF được phép.
- Container được tách cho từng solver nhưng cấu hình Docker nới quyền và bật mạng; không coi đây là ranh giới an toàn để chạy payload không tin cậy trên máy quan trọng.
- Khi chạy chế độ đầy đủ, coordinator và nhiều solver có thể hoạt động liên tục, phát sinh tài nguyên và chi phí. Bắt đầu với `--max-challenges 1`, một model, `--no-submit`; theo dõi log và dừng bằng `Ctrl+C` khi cần.
- Gateway 9router cho coordinator và solver chưa được xác minh end-to-end với Docker trong môi trường hiện tại. Không coi các test cấu hình là chứng nhận tương thích với mọi router/model.

## Ghi nhận

- [es3n1n/Eruditus](https://github.com/es3n1n/Eruditus) — phần tương tác CTFd và trợ giúp HTML trong `pull_challenges.py` dựa trên dự án này.
