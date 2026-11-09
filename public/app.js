'use strict';
const form = document.getElementById('link-form');
const input = document.getElementById('video-url');
const message = document.getElementById('message');
const result = document.getElementById('result');
const inspectButton = document.getElementById('inspect-btn');
const pasteButton = document.getElementById('paste-btn');
const jobStatus = document.getElementById('job-status');
const jobText = document.getElementById('job-text');
const fileReady = document.getElementById('file-ready');
const fullButton = document.getElementById('full-download');
const videoButton = document.getElementById('download-video');
const audioButton = document.getElementById('download-audio');
let activeUrl = '';
let busy = false;
let pollTimer = null;

document.getElementById('year').textContent = String(new Date().getFullYear());
function setMessage(text, type = 'error') {
  message.hidden = !text;
  message.textContent = text;
  message.className = 'message' + (type === 'info' ? ' info' : '');
}
function setBusy(loading) {
  busy = loading;
  inspectButton.disabled = loading;
  const label = inspectButton.querySelector('span');
  label.textContent = loading ? 'Đang tìm nội dung...' : 'Kiểm tra & tải xuống';
}
function validateLink(text) {
  const match = text.match(/https?:\/\/[^\s<>"']+/i);
  if (!match) throw new Error('Hãy dán liên kết video TikTok hoặc Douyin.');
  let url;
  try { url = new URL(match[0].replace(/[.,;!?)]*$/, '')); }
  catch { throw new Error('Liên kết không hợp lệ.'); }
  if (!/^(https?):$/.test(url.protocol) || !/(^|\.)(tiktok\.com|douyin\.com|iesdouyin\.com)$/i.test(url.hostname)) {
    throw new Error('Chỉ nhận liên kết TikTok hoặc Douyin.');
  }
  return text;
}
async function api(path, body) {
  let response;
  try {
    response = await fetch(path, {
      method: body ? 'POST' : 'GET',
      headers: body ? {'Content-Type': 'application/json'} : {},
      body: body ? JSON.stringify(body) : undefined,
      cache: 'no-store'
    });
  } catch {
    throw new Error('Không kết nối được máy chủ. Hãy kiểm tra mạng hoặc backend.');
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Máy chủ gặp lỗi. Vui lòng thử lại.');
  return data;
}
function showResult(data) {
  document.getElementById('video-title').textContent = data.title || 'Bài đăng TikTok';
  document.getElementById('video-creator').textContent = data.creator || 'TikTok';
  document.getElementById('video-origin').textContent = data.source || 'TikTok';
  const duration = document.getElementById('video-duration');
  if (Number.isFinite(data.duration) && data.duration > 0) {
    const seconds = Math.round(data.duration);
    duration.textContent = `⏱ ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  } else { duration.textContent = 'Bài đăng công khai'; }
  const thumb = document.getElementById('video-thumb');
  const placeholder = document.getElementById('thumb-placeholder');
  thumb.hidden = true;
  placeholder.hidden = false;
  if (data.thumbnail && /^https:\/\//i.test(data.thumbnail)) {
    thumb.onload = () => {thumb.hidden = false; placeholder.hidden = true;};
    thumb.onerror = () => {thumb.hidden = true; placeholder.hidden = false;};
    thumb.src = data.thumbnail;
  }
  jobStatus.hidden = true;
  fileReady.hidden = true;
  result.hidden = false;
  result.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}
form.addEventListener('submit', async e => {
  e.preventDefault();
  if (busy) return;
  setMessage('');
  result.hidden = true;
  if (pollTimer) clearTimeout(pollTimer);
  const text = input.value.trim();
  try { validateLink(text); } catch (err) { setMessage(err.message); return; }
  setBusy(true);
  try {
    const data = await api('/api/inspect', {url: text});
    activeUrl = text;
    showResult(data);
  } catch (err) { setMessage(err.message); }
  finally {setBusy(false);}
});
pasteButton.addEventListener('click', async () => {
  try {
    const value = await navigator.clipboard.readText();
    input.value = value;
    input.focus();
    setMessage('');
  } catch {
    input.focus();
    setMessage('Trình duyệt chưa cho phép đọc bộ nhớ tạm. Nhấn giữ ô nhập rồi chọn “Dán”.', 'info');
  }
});
document.getElementById('reset-btn').addEventListener('click', () => {
  result.hidden = true;
  jobStatus.hidden = true;
  fileReady.hidden = true;
  fullButton.hidden = true;
  if (pollTimer) clearTimeout(pollTimer);
});
function setDownloadBusy(value) {
  videoButton.disabled = value;
  audioButton.disabled = value;
  jobStatus.hidden = !value;
}
async function startDownload(kind, mode) {
  if (!activeUrl || videoButton.disabled) return;
  setMessage('');
  fileReady.hidden = true;
  fullButton.hidden = true;
  setDownloadBusy(true);
  jobText.textContent = 'Đang chuẩn bị tải ' + (kind === 'audio' ? 'MP3' : 'video') + '...';
  try {
    const payload = {url: activeUrl, type: kind};
    if (mode === 'full') payload.mode = 'full';
    const started = await api('/api/download', payload);
    let checks = 0;
    const poll = async () => {
      // Stop after about 3 minutes; the backend has its own 180s timeout.
      if (++checks > 95) throw new Error('Xử lý quá lâu, vui lòng thử lại.');
      const status = await api('/api/jobs/' + encodeURIComponent(started.job_id));
      if (status.status === 'ready') {
        setDownloadBusy(false);
        // V2: stream jobs carry stream_url; file jobs carry download_url.
        fileReady.href = status.stream_url || status.download_url;
        fileReady.download = 'HimTik-' + kind;
        fileReady.hidden = false;
        if (status.stream_url) {
          fullButton.hidden = false;
          setMessage('Video đã sẵn sàng. Chạm nút xanh để mở video; trên iPhone dùng Chia sẻ → Lưu video. Nếu video không mở được, bấm “Tải bản đầy đủ”.', 'info');
        } else {
          setMessage('Tệp đã sẵn sàng. Chạm nút màu xanh bên dưới để tải, đặc biệt trên iPhone.', 'info');
        }
        return;
      }
      if (status.status === 'error') throw new Error(status.error || 'Không thể tải tệp.');
      jobText.textContent = status.status === 'queued' ? 'Đang chờ lượt xử lý...' : 'Đang tải và xử lý tệp, vui lòng giữ trang mở...';
      pollTimer = setTimeout(async () => {
        try { await poll(); } catch (err) { setDownloadBusy(false); setMessage(err.message); }
      }, 1800);
    };
    await poll();
  } catch (err) { setDownloadBusy(false); setMessage(err.message); }
}
videoButton.addEventListener('click', () => startDownload('video'));
audioButton.addEventListener('click', () => startDownload('audio'));
fullButton.addEventListener('click', () => startDownload('video', 'full'));
