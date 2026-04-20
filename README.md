# BulkSender Pro — Python/Flask Edition

Python + Flask conversion of the original Node.js WhatsApp bulk sender.

## Stack
- **Backend**: Python 3.12, Flask, Flask-SocketIO
- **WhatsApp**: Playwright (Chromium) driving WhatsApp Web
- **Frontend**: Vanilla HTML/CSS/JS with Socket.IO

## Project Structure
```
bulksender/
├── server.py            # Flask app + SocketIO (replaces server.js)
├── whatsapp_client.py   # WhatsApp Web client (replaces whatsapp-web.js)
├── requirements.txt
├── Dockerfile
├── render.yaml
└── app/
    ├── login.html       # QR scan page
    └── dashboard.html   # Bulk send UI
```

## Local Development

### 1. Install dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Run
```bash
python server.py
```
Open http://localhost:3000

## Deploy to Render

1. Push to GitHub
2. Create a new Render service → "Deploy from repo"
3. Select **Docker** runtime
4. Render will use `render.yaml` automatically
5. Add a **Persistent Disk** at `/data` (keeps your WhatsApp session)

## Environment Variables
| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3000` | Server port |
| `RENDER` | `false` | Set `true` on Render |
| `DOCKER` | `false` | Set `true` in Docker |
| `SECRET_KEY` | auto | Flask secret key |
| `PUPPETEER_EXECUTABLE_PATH` | `/usr/bin/google-chrome-stable` | Chrome path |

## Notes
- WhatsApp session is saved in `/data/.wwebjs_auth` (Render) or `./sessions` (local)
- After scanning QR once, session persists across restarts (if using Persistent Disk)
- Same ban risk as the Node.js version — uses unofficial WhatsApp Web protocol
