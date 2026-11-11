# Ghi chú phân tích · Day 04

Họ và tên: Trieu Hai Dang Trinh
MSSV: 2A202603874

## 1. Case được chọn

- ID case: A5
- Câu trả lời của agent: Agent từ chối cung cấp danh sách đầy đủ
  incident GitHub Actions năm 2025 vì tool chỉ tìm trong 50 incident gần
  nhất. Câu trả lời nêu snapshot `2026-11-09 03:50 UTC`, 23 kết quả khớp
  Actions, không bịa danh sách năm 2025, nhưng chỉ dẫn trang
  `https://www.githubstatus.com` thay vì các URL incident đã đọc.
- Điểm ở lần chạy 1: 99/100 trong `eval_before.json`; full baseline
  đạt 97.14/100, 22/22 case `scored`.

## 2. Bằng chứng trong trace

Dán đoạn trace chứng minh lỗi và nêu nguyên nhân có thể xảy ra.

```text
A5 step 3: get_incident_details thành công, url=https://stspg.io/lv4q3xm9r0rk
A5 step 4: get_incident_details thành công, url=https://stspg.io/7pwv28hvkmz8
A5 step 5: get_incident_details thành công, url=https://stspg.io/pjgptg3cfc6n
A5 step 6: model finish=stop, tool_calls=0
result.status=completed
final answer chỉ chứa nguồn chung https://www.githubstatus.com
```

Trace cho thấy loop đã có ba URL nguồn cụ thể từ tool thành công nhưng
vẫn chấp nhận final answer thiếu cả ba URL. Hai test validation trước thay
đổi cũng chứng minh cùng nguyên nhân: loop coi `finish == 'stop'` là
`completed` mà không kiểm tra evidence đã quan sát. Đây là lỗi ở loop,
không phải do tool thiếu dữ liệu. Judge vẫn cho A5 99 điểm vì nội dung
thận trọng, cho thấy điểm judge không thay thế được validation cơ học.

## 3. Thay đổi đã thực hiện

- Sửa gì, ở đâu: Trong `agent.py`, lưu các `url` duy nhất từ
  kết quả tool có `ok == true`. Trước khi trả `completed`, kiểm tra final
  answer có đủ các URL này hay không. Nếu thiếu, ghi event
  `validation_missing_source`; khi còn step thì yêu cầu model viết lại và dẫn
  đủ URL, còn khi hết budget thì kết thúc `max_steps` thay vì giả vờ
  `completed`. Kết quả tool thất bại không tạo yêu cầu nguồn.
- Vì sao chọn thay đổi này: Đây là gate nhỏ, xác định được và
  không hard-code case hay URL. Nó giải quyết trực tiếp lỗi A5 và contract
  trong `tests/test_validation.py`, đồng thời giữ nguyên tool-call IDs,
  multiple tool calls, trace và `max_steps`.

## 4. So sánh hai lần chạy

| Chỉ số | `eval_before.json` | `eval_after.json` |
|---|---|---|
| Điểm tổng | 97.14/100 | 96.55/100 |
| Điểm case đã chọn (A5) | 99/100 | 98/100 |
| Số model calls | 63 | 77 |
| Token, chi phí nếu có | 111,083 token; $0.016109 | 148,474 token; $0.016635 |

- Cấu hình hai file giống hệt nhau: OpenRouter,
  `openai/gpt-6-luna` cho agent và judge, `max_steps=8`, dataset `core`.
  Cả hai có 22/22 case `scored`, 0 `unscored` và 0 mechanical
  violation. Hash SHA-256 của baseline trước và sau CP6 vẫn là
  `5BAA51154D234AE76872ED6A167055FCC415D42389BD4F4D5A70FA3A7B51C324`.
- Chi tiết chi phí: agent/judge lần trước là 65,011/46,072 token
  với chi phí $0.009168/$0.006940; lần sau là 99,698/48,776 token
  với chi phí $0.009408/$0.007227. Tool calls tăng 60 -> 64,
  tool steps tăng 41 -> 51. Evidence Gate phát 4 event ở lần sau.
- Lỗi đã giải quyết: Ở A5, gate phát hiện final answer thiếu bốn
  URL tại step 6, yêu cầu revision, và agent hoàn tất ở step 8 với
  đủ bốn URL incident. Agent không còn đánh dấu `completed` ngay khi
  thiếu evidence. Gate cũng kích hoạt ở E4, H5 và H6.
- Case tăng đáng chú ý: E1 +9, A8 +3, E3/A6/H6 +2. Case giảm
  đáng chú ý: E5 -13, H1 -7, H8 -6; E4 và A1 -2. A5 giảm 1
  điểm dù hành vi dẫn nguồn đã được sửa. Điểm tổng giảm 0.59
  điểm; không có bằng chứng rằng các thay đổi điểm này đều do
  Evidence Gate thay vì dao động model/judge.
- Lỗi còn lại hoặc mới xuất hiện: Gate làm tăng model calls và
  context khi model đọc nhiều incident. Nếu model dùng gần hết budget cho tool,
  revision có thể không còn đủ bước; contract khi đó cố ý trả
  `max_steps` thay vì `completed`.

## 5. Giới hạn

- LLM judge là phép đánh giá xấp xỉ và dùng cùng model với agent,
  nên có thể thiên lệch hoặc thay đổi cách diễn giải giữa các run.
  Lần CP6 đầu tiên không hợp lệ vì judge của A6 và H1 trả thêm
  dữ liệu sau JSON; hai targeted run đều `scored` 91, sau đó full run
  được chạy lại nguyên vẹn. Không có kết quả targeted nào được ghép vào
  `eval_after.json`.
- Snapshot được chụp ngày 09/11/2026 và tool chỉ tìm trong 50 incident
  gần nhất; nó không phản ánh trạng thái live hay lịch sử đầy đủ.
- Mỗi phiên bản chỉ có một full run hợp lệ. Chênh lệch 97.14 -> 96.55
  không chứng minh chất lượng ổn định tăng hay giảm; cần nhiều run cùng
  cấu hình và phân tích phương sai để kết luận.
- Evidence Gate chỉ xác nhận URL đã xuất hiện trong final answer. Nó
  không chứng minh mọi claim được URL hỗ trợ, nguồn là chính xác, hay
  model đã diễn giải đúng phạm vi và timeline.
