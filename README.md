# HimTik Downloader

Trang tải video TikTok/Douyin/Facebook/Instagram **bản V2 thực sự có backend**, tối ưu màn hình iPhone. Không phải trang tĩnh giả nút tải.

V2 ưu tiên tốc độ: video được **truyền trực tiếp (stream)** từ nguồn qua backend thay vì bắt buộc tải toàn bộ về máy chủ trước — metadata được cache 10 phút nên link tải lại gần như tức thì. Khi không truyền trực tiếp được, hệ thống tự fallback sang cách tải cũ.

## Có gì?

- Giao diện tiếng Việt, responsive, Liquid Glass tối, không cần CDN/font ngoài.
- Dán link bài đăng công khai, xem trước tiêu đề, tác giả, thumbnail và thời lượng (nếu dữ liệu có sẵn).
- Tải video nguồn có sẵn (ưu tiên MP4), tách MP3 bằng FFmpeg.
- **Tải ảnh (V2.0.4):** slideshow/album ảnh và ảnh đơn từ TikTok/Instagram/Facebook. Mỗi ảnh được trả riêng (không nén zip) để mở rồi nhấn giữ lưu thẳng vào ứng dụng Ảnh trên iPhone. Tối đa 50 ảnh/album, tổng tối đa 120 MB.
- **Tải ảnh Instagram/Facebook bằng gallery-dl (V2.0.5):** yt-dlp không đọc được bài ảnh IG ("No video formats found") và FB bắt đăng nhập, nên link IG/FB đi qua gallery-dl. Một ảnh trả thẳng file (`files: [{name, url: "/api/files/{id}/0"}]`); nhiều ảnh nén thành `photos.zip` và trả qua `download_url: "/api/files/{id}"`. Tối đa 30 ảnh/album. Job trả thêm `found` (số ảnh phát hiện), `downloaded` (số ảnh tải được) và `warning` khi tải thiếu (vd `"Chỉ tải được 3/8 ảnh."`). Không có cookie thì IG/FB thường báo lỗi rõ `"Bài này cần đăng nhập, chưa có cookie"` — muốn tải được thì đặt biến môi trường `IG_COOKIES` / `FB_COOKIES` (nội dung cookie định dạng Netscape) trong Render Dashboard; server chỉ ghi ra file tạm lúc chạy rồi xóa ngay, không in ra log, không lưu vào repo. TikTok/Douyin giữ nguyên đường yt-dlp cũ.
- **Bỏ zip + parser Facebook (V2.0.6):** nhiều ảnh IG/FB nay cũng trả từng file riêng qua `files` (không nén zip nữa) để phím tắt iPhone lưu thẳng từng ảnh vào album. Facebook: gallery-dl không đọc được một số trang ảnh (crash `KeyError 'fbid'`), nên khi gallery-dl thất bại server tự thử đường parser — tải trang bài đăng bằng cookie FB rồi lấy `og:image` (chỉ được 1 ảnh chính của bài; bài video thì báo lỗi video, không trả thumbnail).
- **Parser Facebook V2.0.7:** parser gửi header như trình duyệt (Accept/Accept-Language) vì Facebook trả trang rỗng cho request trần; lỗi của parser hiện rõ (tải trang / không có og:image / tải ảnh) thay vì bị lỗi chung của gallery-dl che đi; không có og:image mà trang là trang đăng nhập thì báo lỗi cookie hết hạn; `og:image:secure_url` làm dự phòng.
- **Parser Facebook V2.0.8:** Cookie-Editor xuất domain `.facebook.com` với cờ FALSE, CPython `MozillaCookieJar` từ chối cả file vì vậy (parser 2.0.7 chết ở bước đọc cookie: `"Không đọc được cookie Facebook."`). Parser nay tự đọc file cookie Netscape (bỏ dòng lỗi, chỉ gửi cookie đúng host/path qua header `Cookie`), không dùng `MozillaCookieJar` nữa.
- **Parser Facebook V2.0.9 (chẩn đoán):** khi trang không có og:image, lỗi ghi thêm tiêu đề trang (`(Trang: ...)`) để biết Facebook trả trang gì; khi tải trang lỗi HTTP thì ghi mã trạng thái (`HTTP 404`).
- **Parser Facebook V2.0.10 (chẩn đoán):** lỗi parser ghi thêm dòng `[Chẩn đoán: ...]` — URL cuối sau redirect, mã HTTP, độ dài body, tên cookie đã gửi (có c_user/xs không, tên nào trùng, độ dài header), USER_ID có trong trang không và có khớp c_user không (chỉ đúng/sai). **Không bao giờ ghi giá trị cookie hay nội dung trang.** Đã kiểm chứng: trang FB chưa đăng nhập KHÔNG có trường USER_ID nào (0 lần) nên `"USER_ID":"0"` không phải marker chưa đăng nhập; gallery-dl -v cho thấy nó đổi photo.php thành `/photo/?fbid=` và lấy được trang login 200 (urllib bị 400), còn /posts/ thì 200 rồi crash KeyError 'fbid' (lỗi extractor).
- **Parser Facebook V2.0.11 (sửa theo log 2.0.10):** log live cho thấy session ĐƯỢC nhận (URL cuối /photo.php, HTTP 200, body 1.1 MB, c_user + xs có, USER_ID khớp c_user) nhưng trang không có og:image — ảnh nằm trong JSON của `<script>` nên parser fallback sang mẫu `,"image":{"uri":"..."` (như gallery-dl), ưu tiên ảnh lớn nhất theo width×height. Thêm header Sec-Fetch-Dest/Mode/Site như gallery-dl (đã kiểm chứng local: /posts/ từ 400 thành 200 khi thêm Sec-Fetch) để sửa lỗi HTTP 400.
- **Parser Facebook V2.0.12 (sửa theo log 2.0.11 + HAND OFF 2):** 2.0.11 vẫn không ra ảnh — trang có session nhưng KHÔNG có og:image lẫn mẫu `,"image":{"uri":"..."`. Parser mới unescape toàn bộ trang (\/ → /, \u0026 → &, &amp; → &) rồi tìm mọi link scontent/fbcdn, ưu tiên khối JSON chứa đúng fbid của ảnh đích, chọn bản lớn nhất, loại avatar/quảng cáo/reaction/emoji/sticker/đề xuất. Link /posts/ nhiều ảnh: lấy danh sách fbid ảnh đính kèm trong bài rồi gọi từng trang photo.php?fbid= để lấy ảnh gốc, thiếu thì cảnh báo found/downloaded. Chế độ debug chỉ bật bằng biến môi trường HIMTIK_FB_DEBUG (tắt mặc định): đếm scontent trong HTML thô và sau unescape, in tối đa 10 mẫu (khóa JSON, host+path, width/height, che query string) và fbid có trong trang không — **không ghi cookie, không ghi HTML thô**. gallery-dl với cùng cookie cũng không lấy được ảnh 2 link FB này (đã kiểm chứng live), nên vẫn dùng parser riêng cho Facebook.
- **Parser Facebook V2.0.13 (HAND OFF 3 bước 1):** log 2.0.12 cho thấy trang FB chỉ là "vỏ" app web (18 link fbcdn toàn tài nguyên giao diện, không có ảnh bài đăng hay fbid) — nội dung nạp sau bằng GraphQL qua JS, parser HTML tĩnh không lấy được. Khi gallery probe thất bại và parser cũng thất bại, lỗi cuối kèm log gallery-dl -v đã che cookie (chỉ khi HIMTIK_FB_DEBUG bật). Đã kiểm chứng local: UA facebookexternalhit KHÔNG cookie cũng không lấy được og:image (photo.php redirect về login, /posts/ không có og:image) nên bước 2 của HAND OFF 3 không làm được từ phía server.
- **FetchSocial fallback V2.0.14:** ảnh Facebook nay có đường thứ ba — FetchSocial trên RapidAPI (gói Basic miễn phí, 50 request/tháng). Khi gallery-dl và parser đều thất bại, server gọi FetchSocial lấy `downloadUrlList` rồi tải từng ảnh. Chỉ dùng cho link dạng `/share/p/` (đã kiểm chứng live 2026-11-19: link share trả 4 ảnh thật, còn `/posts/` và photo.php trả `data:null`); link khác và khi chưa đặt key thì bỏ qua. Key đặt trong biến môi trường `RAPIDAPI_KEY` trên Render (không trong repo, không trong chat). Key sai/hết lượt miễn phí thì lỗi báo rõ (`FetchSocial: key RapidAPI...` / `đã hết lượt miễn phí trong tháng`). Avatar tác giả (trường `avatar` riêng) không bao giờ bị tải nhầm.
- **V2 — video stream trực tiếp:** `POST /api/download` (video) trả job `ready` kèm `stream_url` ngay khi tìm được link MP4 trực tiếp; trình duyệt mở `GET /api/stream/{id}` và nhận video theo luồng, không chờ máy chủ tải xong. Stream chạy bằng yt-dlp được nạp sẵn metadata đã cache (`--load-info-json`) nên mỗi link chỉ trích xuất một lần — không thử tải trực tiếp link CDN bằng HTTP thường nữa vì TikTok chặn kiểu tải đó từ IP máy chủ. Nếu stream lỗi thì server trả `fallback: "full"` để client dùng bản tải đầy đủ (`/api/files/{id}`).
- **V2 — cache metadata:** mỗi link chỉ chạy `yt-dlp --dump-single-json` một lần trong 10 phút; lần tải lại sau dùng cache.
- Hàng đợi tải, trạng thái và thông báo lỗi; tệp tạm tự xóa sau khoảng 20 phút.
- Chỉ cho phép link TikTok/Douyin/Facebook/Instagram; giới hạn 120 MB/tệp, không giới hạn tốc độ, không lấy thông tin đăng nhập.

**Không hỗ trợ trong V2:** hàng loạt, video riêng tư, lưu trực tiếp vào Thư viện Ảnh iPhone (iOS bắt buộc thao tác Chia sẻ → Lưu video), cam kết tải không logo/4K mọi bài đăng, tua/seek (Range) khi xem stream. Nguồn media phụ thuộc yt-dlp và mức truy cập TikTok/Douyin/Facebook/Instagram tại thời điểm chạy.

## Chạy thử trên máy tính

Cần Python 3.12+, `yt-dlp`, `gallery-dl` và `ffmpeg` cài trong PATH:

```sh
python -m pip install -U yt-dlp gallery-dl
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

`public/index.html` giao diện, `public/style.css` theme, `public/app.js` xử lý fetch/polling, `server.py` HTTP server / queue / yt-dlp / gallery-dl, `Dockerfile` cài ffmpeg, yt-dlp và gallery-dl, `render.yaml` deploy.

API: `POST /api/inspect`, `POST /api/download`, `GET /api/jobs/{id}`, `GET /api/files/{id}`, `GET /api/files/{id}/{index}`, `GET /api/stream/{id}`, `GET /api/health`.

Luồng video V2: `POST /api/download {"url", "type": "video"}` → job `ready` + `stream_url` (hoặc `queued` nếu phải tải đầy đủ) → `GET /api/stream/{id}` truyền MP4 trực tiếp (`Content-Disposition: inline`, kiểm tra magic `ftyp` trước khi gửi header) → lỗi thì client gọi lại `POST /api/download {"mode": "full"}` rồi lấy `GET /api/files/{id}`. MP3 luôn đi đường tải đầy đủ.

Luồng ảnh (V2.0.4): `POST /api/download {"url", "type": "image"}` → server kiểm tra trước bằng `inspect_media` (chế độ ảnh, không dùng cache): bài video thì trả 422 "Bài đăng này là video, hãy chọn loại Video."; bài ảnh thì tạo job tải đầy đủ (không stream, không zip) → `GET /api/jobs/{id}` trả `files: [{name, url: "/api/files/{id}/0", bytes}, ...]` → mỗi ảnh lấy riêng qua `GET /api/files/{id}/{index}` (`Content-Disposition: inline`, `Content-Type` theo đuôi ảnh) để mở trực tiếp rồi nhấn giữ lưu vào Ảnh. `POST /api/inspect` nhận thêm `"type": "image"` để trả trường `images` (số ảnh trong bài).

Luồng ảnh Instagram/Facebook (V2.0.5 gallery-dl, V2.0.6 bỏ zip + parser FB): `POST /api/download {"url", "type": "image"}` với link IG/FB bỏ qua bước kiểm tra yt-dlp, tạo job ngay → worker chạy gallery-dl (probe `--get-urls` đếm số ảnh, rồi tải vào thư mục tạm riêng của job; tối đa 30 ảnh; timeout probe 60s, tải 180s) → `GET /api/jobs/{id}` trả `found`, `downloaded` và `warning` (nếu tải thiếu); mọi ảnh đều trả riêng qua `files: [{name, url: "/api/files/{id}/{index}", bytes}]` (không zip) để lưu thẳng vào album iPhone. Facebook: nếu gallery-dl không đọc được trang ảnh thì server thử đường parser — tải trang bằng cookie FB rồi lấy `og:image` (chỉ 1 ảnh chính; bài video thì báo lỗi video). Bài cần đăng nhập thì job báo lỗi `"Bài này cần đăng nhập, chưa có cookie"` (hoặc `"...cookie có thể đã hết hạn."` khi đã có cookie mà vẫn bị từ chối); bài video thì báo `"Bài đăng này là video, hãy chọn loại Video."`.
