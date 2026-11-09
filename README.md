# HimTik Downloader

Trang tải video TikTok/Douyin **bản MVP thực sự có backend**, tối ưu màn hình iPhone. Không phải trang tĩnh giả nút tải.

## Có gì?

- Giao diện tiếng Việt, responsive, Liquid Glass tối, không cần CDN/font ngoài.
- Dán link bài đăng công khai, xem trước tiêu đề, tác giả, thumbnail và thời lượng (nếu dữ liệu có sẵn).
- Tải video nguồn có sẵn (ưu tiên MP4), tách MP3 bằng FFmpeg.
- Hàng đợi tải, trạng thái và thông báo lỗi; tệp tạm tự xóa sau khoảng 20 phút.
- Chỉ cho phép link TikTok/Douyin; giới hạn 120 MB/tệp, giới hạn tốc độ đơn giản, không lấy thông tin đăng nhập.

**Không hỗ trợ trong MVP:** album ảnh/slideshow, hàng loạt, video riêng tư, lưu trực tiếp vào Thư viện Ảnh iPhone, cam kết tải không logo/4K mọi bài đăng. Nguồn media phụ thuộc yt-dlp và mức truy cập TikTok/Douyin tại thời điểm chạy.

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
- URL nguồn chỉ chấp nhận TikTok/Douyin. `yt-dlp` có thể tự đi theo redirect của nguồn; trước khi mở dịch vụ cho người lạ, cần thêm kiểm soát redirect/SSRF ở tầng mạng, proxy trusted, WAF/rate limit theo IP và theo dõi lưu lượng/CPU/ổ đĩa.
- Trạng thái jobs đang được lưu trong RAM của **một tiến trình**. Render khởi động lại thì job mất. Để scale nhiều máy, cần Redis/object storage.
- Máy chủ giới hạn tệp đầu ra 120 MB, nhưng file tạm của bộ giải mã vẫn có thể vượt quá hạn mức trước khi cleanup. Cần giới hạn container disk/CPU khi triển khai lớn.
- Không bảo đảm xóa watermark; chỉ tải bản nguồn khả dụng. Không dùng công cụ để vượt giới hạn riêng tư hay sao chép nội dung khi chưa được phép.
- Nếu TikTok chặn dải IP máy chủ hoặc thay đổi API, extraction có thể thất bại; cập nhật `yt-dlp` và đánh giá lại.

## Kiến trúc

`public/index.html` giao diện, `public/style.css` theme, `public/app.js` xử lý fetch/polling, `server.py` HTTP server / queue / yt-dlp, `Dockerfile` cài ffmpeg và yt-dlp, `render.yaml` deploy.

API: `POST /api/inspect`, `POST /api/download`, `GET /api/jobs/{id}`, `GET /api/files/{id}`, `GET /api/health`.
