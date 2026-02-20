# Hướng dẫn cài đặt Tapo Cloud Recorder → Google Drive

Script `tapo_drive_recorder.py` ghi hình liên tục từ camera Tapo C202 qua Cloud Relay, chia thành các chunk 60 giây và tự động upload lên Google Drive. Sau khi upload thành công, file local sẽ được xóa để tiết kiệm dung lượng — phù hợp cho VPS RAM thấp (1GB).

---

## 1. Yêu cầu

- Python 3.9+
- Tài khoản Google (Gmail)
- Một Google Cloud Project (miễn phí)

## 2. Cài đặt dependencies

```bash
cd tapo-cli
pip3 install -r requirements.txt
```

Các package Google Drive cần thiết đã có trong `requirements.txt`:
- `google-api-python-client`
- `google-auth`
- `google-auth-httplib2`
- `google-auth-oauthlib`

## 3. Tạo Google Service Account

Service Account cho phép script upload lên Drive mà **không cần mở browser đăng nhập** — lý tưởng cho VPS/headless server.

### Bước 3.1: Tạo Project trên Google Cloud Console

1. Truy cập [Google Cloud Console](https://console.cloud.google.com/)
2. Click **Select a project** → **New Project**
3. Đặt tên (ví dụ: `TapoCam`) → **Create**

### Bước 3.2: Bật Google Drive API

1. Trong project vừa tạo, vào **APIs & Services** → **Library**
2. Tìm **Google Drive API** → Click **Enable**

### Bước 3.3: Tạo Service Account & Key JSON

1. Vào **IAM & Admin** → **Service Accounts**
2. Click **Create Service Account**
   - Tên: `tapo-uploader` (hoặc tên tùy ý)
   - Click **Create and Continue** → **Done**
3. Click vào service account vừa tạo → tab **Keys**
4. **Add Key** → **Create new key** → chọn **JSON** → **Create**
5. File JSON sẽ tự download — **đặt file này vào thư mục `tapo-cli/`** với tên:

```
gdrive_service_account.json
```

> ⚠️ **Bảo mật**: Không commit file này lên git. File đã được thêm vào `.gitignore`.

### Bước 3.4: Lấy email Service Account

Trong trang Service Accounts, copy email có dạng:
```
tapo-uploader@your-project.iam.gserviceaccount.com
```

## 4. Tạo & chia sẻ thư mục Google Drive

1. Mở [Google Drive](https://drive.google.com/) bằng tài khoản Google cá nhân
2. Tạo thư mục mới (ví dụ: `TapoCam`)
3. Click chuột phải → **Share** (Chia sẻ)
4. Paste email Service Account (bước 3.4) → chọn quyền **Editor** → **Send**
5. Mở thư mục đó, copy **Folder ID** từ URL:

```
https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz
                                        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                        ↑ Đây là FOLDER_ID
```

## 5. Cấu hình và chạy

### Cách 1: Dùng biến môi trường (khuyên dùng)

```bash
export GOOGLE_DRIVE_FOLDER_ID="1AbCdEfGhIjKlMnOpQrStUvWxYz"
python3 tapo_drive_recorder.py
```

Nếu file JSON key đặt ở vị trí khác:

```bash
export GOOGLE_DRIVE_SA_FILE="/đường/dẫn/tới/key.json"
export GOOGLE_DRIVE_FOLDER_ID="1AbCdEfGhIjKlMnOpQrStUvWxYz"
python3 tapo_drive_recorder.py
```

### Cách 2: Chạy bằng systemd (cho VPS, chạy 24/7)

Tạo file `/etc/systemd/system/tapo-recorder.service`:

```ini
[Unit]
Description=Tapo Camera Recorder + Google Drive Upload
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/tapo-cli
Environment=GOOGLE_DRIVE_FOLDER_ID=1AbCdEfGhIjKlMnOpQrStUvWxYz
ExecStart=/usr/bin/python3 /root/tapo-cli/tapo_drive_recorder.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Sau đó:

```bash
sudo systemctl daemon-reload
sudo systemctl enable tapo-recorder
sudo systemctl start tapo-recorder

# Xem log:
sudo journalctl -u tapo-recorder -f
```

## 6. Cấu trúc file trên Google Drive

Script sẽ tự tạo subfolder theo ngày:

```
TapoCam/                        ← Folder bạn chia sẻ
├── 2026-02-20/
│   ├── 2026-02-20_10-00-00.ts
│   ├── 2026-02-20_10-01-00.ts
│   └── ...
├── 2026-02-21/
│   └── ...
```

Mỗi file `.ts` là một đoạn video 60 giây (có thể thay đổi `CHUNK_DURATION` trong script).

## 7. Xử lý sự cố

| Vấn đề | Giải pháp |
|--------|-----------|
| `Missing Google Drive dependencies` | Chạy `pip3 install -r requirements.txt` |
| `service account JSON not found` | Kiểm tra file `gdrive_service_account.json` có đúng vị trí |
| `GOOGLE_DRIVE_FOLDER_ID not set` | Kiểm tra biến môi trường đã export chưa |
| `403 Insufficient Permission` | Đảm bảo đã share folder cho email Service Account với quyền **Editor** |
| `404 File not found` | Kiểm tra lại Folder ID có đúng không |
| Upload failed nhưng file không bị xóa | Đây là cơ chế an toàn — file chỉ xóa khi upload thành công |

## 8. So sánh với Cloudflare R2

| | `tapo_cloud_recorder.py` | `tapo_drive_recorder.py` |
|---|---|---|
| Storage | Cloudflare R2 (trả phí) | Google Drive (15GB miễn phí) |
| Auth | API Key | Service Account JSON |
| Setup | Đơn giản | Cần tạo GCP project |
| Phù hợp | Production, bandwidth lớn | Cá nhân, tiết kiệm chi phí |
