# HimTik Downloader

Trang tải video TikTok/Douyin/Facebook/Instagram **bản V2 thực sự có backend**, tối ưu màn hình iPhone. Không phải trang tĩnh giả nút tải.

V2 ưu tiên tốc độ: video được **truyền trực tiếp (stream)** từ nguồn qua backend thay vì bắt buộc tải toàn bộ về máy chủ trước — metadata được cache 10 phút nên link tải lại gần như tức thì. Khi không truyền trực tiếp được, hệ thống tự fallback sang cách tải cũ.

## Có gì?

- Giao diện tiếng Việt, responsive, Liquid Glass tối, không cần CDN/font ngoài.
- Dán link bài đăng công khai, xem trước tiêu đề, tác giả, thumbnail và thời lượng (nếu dữ liệu có sẵn).
- Tải video nguồn có sẵn (ưu tiên MP4), tách MP3 bằng FFmpeg.
- **V2 — video stream trực tiếp:** `POST /api/download` (video) trả job `ready` kèm `stream_url` ngay khi tìm được link MP4 trực tiếp; trình duyệt mở `GET /api/stream/{id}` và nhận video theo luồng, không chờ máy chủ tải xong. Stream chạy bằng yt-dlp được nạp sẵn metadata đã cache (`--load-info-json`) nên mỗi link chỉ trích xuất một lần — không thử tải trực tiếp link CDN bằng HTTP thường nữa vì TikTok chặn kiểu tải đó từ IP máy chủ. Nếu stream lỗi thì server trả `fallback: "full"` để client dùng bản tải đầy đủ (`/api/files/{id}`).
- **V2 — cache metadata:** mỗi link chỉ chạy `yt-dlp --dump-single-json` một lần trong 10 phút; lần tải lại sau dùng cache.
- Hàng đợi tải, trạng thái và thông báo lỗi; tệp tạm tự xóa sau khoảng 20 phút.
- Chỉ cho phép link TikTok/Douyin/Facebook/Instagram; giới hạn 120 MB/tệp, không giới hạn tốc độ, không lấy thông tin đăng nhập.

**Không hỗ trợ trong V2:** album ảnh/slideshow, hàng loạt, video riêng tư, lưu trực tiếp vào Thư viện Ảnh iPhone (iOS bắt buộc thao tác Chia sẻ → Lưu video), cam kết tải không logo/4K mọi bài đăng, tua/seek (Range) khi xem stream. Nguồn media phụ thuộc yt-dlp và mức truy cập TikTok/Douyin/Facebook/Instagram tại thời điểm chạy.

## Chạy thử trên máy tính

Cần Python 3.12+, `yt-dlp` và `ffmpeg` cài trong PATH:

```sh
python -m pip install -U yt-dlp
python server.py
```

Mở `http://localhost:8000` (mặc định). Không cần Flask hay Node.js.

## Đưa lên mạng cho iPhone: Render (Docker)

1. Giải nén thư mục và đưa **toàn bộ nội dung** lên repository GitHub **mới**. Không cần đụng bất kỳ repo cũ nào.
2. Vào [dashboard.render.com](https://dashboard.render.com/) → **New → Blueprint** → kết nối repository đó. Render đọc `render.yaml` và tạo web service chạy Docker.
3. Deploy xong, mở địa chỉ `https://...onrender.com` bằng Safari iPhone.

Gói Free phù hợp thử nghiệm, có thể sleep sau 15 phút không dùng, lần đầu mở lại chậm. Các hạn mức tài nguyên/băng thông khác áp dụng. Đừng dùng ngay làm dịch vụ công khai có lưu lượng lớn.

## Kiểm thử nhanh không cần TikTok thật

```sh
python -m unittest discover -s tests -v
```

Kiểm thử mock chỉ chứng minh định tuyến và kiểm tra dữ liệu; **không** xác nhận TikTok hiện đang cho tải. Muốn xác nhận tính năng download, chạy hệ thống có yt-dlp + ffmpeg và thử bài đăng TikTok công khai có quyền tải.

## Giới hạn & bảo mật

- Giao diện và API chạy chung domain, tránh lỗi CORS. API cũng cho phép gọi cross-origin từ https://kanz510.github.io (tab tải video trong app đơn hàng). Đừng host HTML thuần trên GitHub Pages và tưởng là tải được; GitHub Pages không chạy backend.
- URL nguồn chỉ chấp nhận TikTok/Douyin/Facebook/Instagram. Stream chạy qua yt-dlp với metadata do chính yt-dlp trích xuất từ link người dùng dán (không tải URL do người dùng tự chỉ định); metadata được cache 10 phút. Trước khi mở dịch vụ cho người lạ vẫn nên cân nhắc thêm WAF/rate limit theo IP và theo dõi lưu lượng/CPU/ổ đĩa.
- Stream không hỗ trợ Range (tua/seek) và không trả Content-Length khi nguồn không khai báo; giới hạn 120 MB được ép bằng bộ đếm byte và deadline 240 giây.
- Trạng thái jobs đang được lưu trong RAM của **một tiến trình**. Render khởi động lại thì job mất. Để scale nhiều máy, cần Redis/object storage.
- Máy chủ giới hạn tệp đầu ra 120 MB, nhưng file tạm của bộ giải mã vẫn có thể vượt quá hạn mức trước khi cleanup. Cần giới hạn container disk/CPU khi triển khai lớn.
- Không bảo đảm xóa watermark; chỉ tải bản nguồn khả dụng. Không dùng công cụ để vượt giới hạn riêng tư hay sao chép nội dung khi chưa được phép.
- Nếu TikTok chặn dải IP máy chủ hoặc thay đổi API, extraction có thể thất bại; cập nhật `yt-dlp` và đánh giá lại.

## Kiến trúc

`public/index.html` giao diện, `public/style.css` theme, `public/app.js` xử lý fetch/polling, `server.py` HTTP server / queue / yt-dlp, `Dockerfile` cài ffmpeg và yt-dlp, `render.yaml` deploy.

API: `POST /api/inspect`, `POST /api/download`, `GET /api/jobs/{id}`, `GET /api/files/{id}`, `GET /api/stream/{id}`, `GET /api/health`.

Luồng video V2: `POST /api/download {"url", "type": "video"}` → job `ready` + `stream_url` (hoặc `queued` nếu phải tải đầy đủ) → `GET /api/stream/{id}` truyền MP4 trực tiếp (`Content-Disposition: inline`, kiểm tra magic `ftyp` trước khi gửi header) → lỗi thì client gọi lại `POST /api/download {"mode": "full"}` rồi lấy `GET /api/files/{id}`. MP3 luôn đi đường tải đầy đủ.
