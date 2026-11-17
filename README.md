# vidgen — Voice-over + text → video minh họa

Công cụ biến kịch bản + giọng đọc (voice-over) thành video có ảnh minh hoạ, hiệu ứng Ken Burns và chuyển cảnh, dành cho kênh YouTube dạng kể chuyện / tài liệu.

Yêu cầu: Python 3.10+ (`pip install requests pillow fastapi uvicorn python-multipart playwright`), ffmpeg/ffprobe trong PATH.
Copy `settings.example.json` thành `settings.json` (hoặc nhập key trong tab ⚙ là tự tạo file).

## GUI (khuyên dùng)

Chạy `start.bat` (hoặc `python app.py`) rồi mở http://127.0.0.1:8765

1. **① Dữ liệu**: tạo project, kéo thả file kịch bản `.md` + các file voice → kiểm tra bảng ghép part ↔ voice → **Phân cảnh**.
2. **② Cảnh & Ảnh**: mỗi cảnh có lời thoại, nút ▶ nghe đúng đoạn voice, từ khoá tìm ảnh, prompt AI.
   - **⚡ Auto-fill**: tự lấy ảnh ngang đầu tiên, chưa trùng, cho mọi cảnh còn thiếu, thử lần lượt các nguồn đã tick.
   - **🔍 Tìm ảnh**: tìm trên một hoặc tất cả nguồn, bấm vào ảnh để chọn · **✨ AI tạo** · **⬆ Tải ảnh lên** · **✕ Xoá**. Kéo thả hoặc Ctrl+V ảnh / link ảnh vào cảnh.
   - **＋** thêm nhiều ảnh cho 1 cảnh (chia đều thời lượng).
   - **✂ Khung hình**: kéo chọn vùng hiển thị, giữ nguyên ảnh + nền mờ, hoặc lia toàn ảnh; hiệu ứng riêng cho từng ảnh. Mặc định: ảnh dọc → nền mờ, ảnh gần vuông → giữ phần trên.
   - **⚡ Xem thử**: xem ngay cảnh đó trên trình duyệt (giọng + ảnh + hiệu ứng), không cần render.
3. **③ Render**: độ phân giải, chuyển cảnh / hiệu ứng (cố định, luân phiên hoặc ngẫu nhiên theo seed), zoom, tiêu đề…
   - **⚡ Xem tức thì** cả part, hoặc **🎬 Render nháp 480p** vài giây để xem đúng kiểu chuyển cảnh.
   - Render chỉ làm lại part có thay đổi; part không đổi giữ bản cũ.
   - **📺 YouTube**: tiêu đề, mô tả, timestamps chương (theo thời lượng thật), tags, credits ảnh → nút copy mô tả hoàn chỉnh (lưu ở `<project>/youtube.json`).
4. **⚙ Nguồn ảnh**: nhập API key (lưu trong `vidgen/settings.json`).

| Nguồn | Key | Ghi chú |
|---|---|---|
| Wikimedia Commons | không | ảnh thật, lịch sử, địa danh; license CC/PD |
| Openverse | không | tổng hợp Flickr + kho CC |
| Pexels / Pixabay / Unsplash | miễn phí, cần đăng ký | ảnh stock đẹp, license thoáng |
| Gemini / ChatGPT / Grok **web** | không (dùng tài khoản đăng nhập) | điều khiển Chrome thật qua CDP, giống google-flow-browser-mcp |
| OpenAI / Gemini API | trả phí / free tier | tạo ảnh AI từ prompt + style chung |

### AI qua web (Gemini / ChatGPT / Grok)
- Tab ⚙ → **Mở trình duyệt + 3 trang để đăng nhập**. Nếu đã có Chrome chạy CDP ở cổng 9222 (ví dụ Chrome của Flow MCP), vidgen dùng lại nó. Nếu chưa, vidgen mở Chrome với profile riêng `vidgen/browser-profile` (chỉ cần đăng nhập lần đầu).
- Mỗi lần tạo ảnh: mở tab mới → gõ prompt → chờ ảnh → lưu → đóng tab. Lỗi thì tab được giữ lại và ảnh chụp màn hình lưu ở `vidgen/data/`.
- Mỗi lần chỉ chạy 1 việc. Khi giao diện các trang đổi, sửa selector trong bảng `SITES` ở [browser_ai.py](browser_ai.py).

Thêm nguồn mới: viết 1 hàm trong `sources.py` và đăng ký vào `SOURCES`.


## Cấu trúc 1 project (mỗi video 1 thư mục)

```
<project>/
  texts/*.md          kịch bản: các mục "## Part 01. Tiêu đề (...)" + marker [pause 0.3s|0.5s|1s]
  voices/part 1.m4a   audio từng part (tên chứa "part N", đuôi m4a/mp3/wav...)
  pics/               ảnh từng cảnh: p01_s01.jpg/png/webp (tự tạo hoặc tự bỏ vào)
  prompts.json        (tự sinh) prompt ảnh cho từng cảnh — sửa thoải mái
  output/plan.json    (tự sinh) timing từng cảnh
  output/video.mp4    video cuối
```

## CLI

```bash
python vidgen.py plan   "monk fruit"     # tách cảnh + căn thời gian theo khoảng lặng trong audio
# (tuỳ chọn) sửa prompts.json cho ảnh đẹp hơn
python vidgen.py images "monk fruit"     # tạo ảnh còn thiếu trong pics/
python vidgen.py render "monk fruit" --titles
# hoặc tất cả 1 lần:
python vidgen.py all "monk fruit" --titles
```

### Cách chia cảnh
- Mỗi `[pause 1s]` = 1 cảnh mới (1 ảnh). Thời điểm đổi ảnh được dò từ khoảng lặng thật trong audio, crossfade rơi đúng vào lúc ngừng nói.
- Cảnh > 16s được tách thêm tại pause nhỏ hơn hoặc khoảng lặng ở cuối câu; cảnh < 3.5s gộp vào cảnh sau.

### Ảnh
- `--provider openai` (cần `OPENAI_API_KEY`) · `placeholder` (nền màu + chữ, để test). Tìm ảnh / AI web dùng GUI.
- Không thích ảnh nào: xoá file đó trong `pics/` (hoặc thay bằng ảnh của bạn cùng tên), sửa prompt, chạy lại `images`.
- `--force` tạo lại toàn bộ.

### Hiệu ứng / tuỳ chọn render
| Tuỳ chọn | Mặc định | Ý nghĩa |
|---|---|---|
| `--zoom` | 1.15 | Mức zoom Ken Burns (zoom in/out, pan trái/phải/lên luân phiên) |
| `--xfade` | 0.8 | Thời lượng crossfade giữa cảnh |
| `--transition` | fade | Kiểu chuyển cảnh ffmpeg xfade (`fade`, `dissolve`, `smoothleft`…) hoặc `random_soft` / `random_all` |
| `--effects` | cycle | Hiệu ứng trong cảnh: `cycle`, `random`, hoặc 1 hiệu ứng cố định (`zoom_in`, `pan_left`…) |
| `--seed` | 1 | Đổi seed = bộ hiệu ứng ngẫu nhiên khác |
| `--force` | tắt | Render lại mọi part kể cả không đổi |
| `--titles` | tắt | Hiện "Part N. Tiêu đề" 4s đầu mỗi part |
| `--gap` | 0.5 | Khoảng nghỉ cuối mỗi part (fade đen) |
| `--parts 3 5` | tất cả | Chỉ render lại part 3 và 5 (rồi ghép lại toàn bộ) |
| `--width/--height/--fps` | 1920/1080/30 | Kích thước video |
| `--preset/--crf` | medium/20 | Chất lượng encode (`veryfast` để render nhanh khi test) |
