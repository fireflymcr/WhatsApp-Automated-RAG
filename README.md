# WhatsApp MCP Server

This is a Model Context Protocol (MCP) server for WhatsApp.

With this you can search and read your personal Whatsapp messages (including images, videos, documents, and audio messages), search your contacts and send messages to either individuals or groups. You can also send media files including images, videos, documents, and audio messages.

It connects to your **personal or business WhatsApp account** directly via the Whatsapp web multidevice API (using the [whatsmeow](https://github.com/tulir/whatsmeow) library). All your messages are stored locally in a SQLite database and only sent to an LLM (such as Claude) when the agent accesses them through tools (which you control).

Here's an example of what you can do when it's connected to Claude.

![WhatsApp MCP](./example-use.png)

> To get updates on this and other projects I work on [enter your email here](https://docs.google.com/forms/d/1rTF9wMBTN0vPfzWuQa2BjfGKdKIpTbyeKxhPMcEzgyI/preview)

> *Caution:* as with many MCP servers, the WhatsApp MCP is subject to [the lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/). This means that project injection could lead to private data exfiltration.

## Installation

### Prerequisites

- Go
- Python 3.6+
- Anthropic Claude Desktop app (or Cursor)
- UV (Python package manager), install with `curl -LsSf https://astral.sh/uv/install.sh | sh`
- FFmpeg (*optional*) - Only needed for audio messages. If you want to send audio files as playable WhatsApp voice messages, they must be in `.ogg` Opus format. With FFmpeg installed, the MCP server will automatically convert non-Opus audio files. Without FFmpeg, you can still send raw audio files using the `send_file` tool.

### Steps

1. **Clone this repository**

   ```bash
   git clone https://github.com/lharries/whatsapp-mcp.git
   cd whatsapp-mcp
   ```

2. **Run the WhatsApp bridge**

   You have two options:

   #### Option A: Docker (recommended — runs 24/7)

   Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/) to be installed and running.

   > [!IMPORTANT]
   > **Database Prerequisite:** The WhatsApp AI Bot requires **Microsoft SQL Server** to store conversation logs and RAG statistics.
   > 1. In **Docker Desktop**, go to **Extensions** in the left sidebar.
   > 2. Search for **SQL Server** or **SQL containers** and install it, or run standard SQL Server on port `14314` (see [DOCKER.md](./DOCKER.md#database-setup-microsoft-sql-server) for full settings).
   > 3. Verify your database connection string in `instances/{instance}/context.yaml`.

   ```bash
   # First run (scan QR code in the terminal)
   docker compose up

   # After QR scan succeeds, Ctrl+C, then run in background
   docker compose up -d
   ```

   The bridge will auto-restart on crashes and after reboots (as long as Docker Desktop is running).

   See [DOCKER.md](./DOCKER.md) for the full Docker setup guide, SQL Server setup, common commands, and re-authentication.

   #### Option B: Manual (Go required)

   Navigate to the whatsapp-bridge directory and run the Go application:

   ```bash
   cd whatsapp-bridge
   go run main.go
   ```

   > **Note:** With this option, the bridge stops when you close the terminal.

   ---

   The first time you run it (either option), you will be prompted to scan a QR code. Scan the QR code with your WhatsApp mobile app to authenticate.

   After approximately 20 days, you may need to re-authenticate.

3. **Connect to the MCP server**

   Add the WhatsApp MCP server to your AI tool's config. Replace the paths below with your actual paths:

   **Windows example:**

   ```json
   {
     "mcpServers": {
       "whatsapp": {
         "command": "C:\\Users\\YOUR_USER\\.local\\bin\\uv.exe",
         "args": [
           "--directory",
           "D:\\whatsapp-mcp\\whatsapp-mcp-server",
           "run",
           "main.py"
         ]
       }
     }
   }
   ```

   **Mac/Linux example:**

   ```json
   {
     "mcpServers": {
       "whatsapp": {
         "command": "uv",
         "args": [
           "--directory",
           "/path/to/whatsapp-mcp/whatsapp-mcp-server",
           "run",
           "main.py"
         ]
       }
     }
   }
   ```

   > **Tip:** On Windows, run `(Get-Command uv).Source` in PowerShell to find your UV path. On Mac/Linux, run `which uv`.

   For **Claude**, save this as `claude_desktop_config.json` at:
   - **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
   - **Mac:** `~/Library/Application Support/Claude/claude_desktop_config.json`

   For **Cursor**, save this as `mcp.json` at:
   - **Windows:** `%USERPROFILE%\.cursor\mcp.json`
   - **Mac/Linux:** `~/.cursor/mcp.json`

4. **Restart Claude Desktop / Cursor**

   Open Claude Desktop and you should now see WhatsApp as an available integration.

   Or restart Cursor.

### Windows Compatibility

If you're running this project on Windows, be aware that `go-sqlite3` requires **CGO to be enabled** in order to compile and work properly. By default, **CGO is disabled on Windows**, so you need to explicitly enable it and have a C compiler installed.

#### Steps to get it working

1. **Install a C compiler**  
   We recommend using [MSYS2](https://www.msys2.org/) to install a C compiler for Windows. After installing MSYS2, make sure to add the `ucrt64\bin` folder to your `PATH`.  
   → A step-by-step guide is available [here](https://code.visualstudio.com/docs/cpp/config-mingw).

2. **Enable CGO and run the app**

   ```bash
   cd whatsapp-bridge
   go env -w CGO_ENABLED=1
   go run main.go
   ```

Without this setup, you'll likely run into errors like:

> `Binary was compiled with 'CGO_ENABLED=0', go-sqlite3 requires cgo to work.`

## Architecture Overview

This application consists of four core components:

1. **Go WhatsApp Bridge** (`whatsapp-bridge/`): A Go application that connects to WhatsApp's web API, handles authentication via QR code, and stores message history in SQLite. It serves as the bridge between WhatsApp and the MCP/AI systems.

2. **Python MCP Server** (`whatsapp-mcp-server/`): A Python server implementing the Model Context Protocol (MCP), providing standardized tools for Claude and other LLM clients to securely interact with WhatsApp.

3. **Python AI Bot Auto-Reply Engine** (`bot/`): A background service using APScheduler that polls for unprocessed incoming messages, classifies queries (e.g. SPAM, SALES, INQUIRY), runs vector/RAG lookups against a SQL Server knowledge base, and auto-generates context-aware customer replies using advanced local reasoning models (Qwen 3.6/3.5).
   - **💰 Auto-Payment Interception & Confirmation:** Automatically intercepts deposit/payment receipt claims, extracts customer details via structured JSON extraction, writes/updates confirmed schedules to the database, and schedules premium HTML customer & admin confirmations via the **Resend API** (with 30-second rate-limiting protection).
   - **📢 Broadcast Marketing Engine:** Triggers and schedules custom bulk marketing campaigns using scheduled cron jobs with automatic daily cap protection.

4. **Premium Web Dashboard** (`dashboard/`): A gorgeous custom web UI for business owners to manage operations, featuring:
   - **💬 Dual-Column WhatsApp Chat logs:** Real-time synchronized chat views with clean green (sent) and gray (received) bubble styling.
   - **🤖 Interactive AI Reply Assistant:** Click to generate a context-aware draft via local RAG/LLM, edit the response, and send it instantly.
   - **📅 Schedule Calendar Dashboard:** Dynamic iCal-compliant calendar showing all bookings, color-coded by status (pending, confirmed).
   - **⚙️ Bot Configuration Panel:** Live hot-reloading configurations for system prompts, Lookback settings, RAG embeddings, and Resend API integrations.

### Data Storage & Databases

- **SQLite Database** (`/data/bridge-store/messages.db`): Lightweight database maintaining local message history logs for the whatsmeow bridge.
- **Microsoft SQL Server Database**: Enterprise-grade database for permanent audit trails, RAG search chunk embeddings, broadcast marketing rules, and `{prefix}_appointments` calendar schedules.

## Usage

Once connected, you can interact with your WhatsApp contacts through Claude, leveraging Claude's AI capabilities in your WhatsApp conversations.

### MCP Tools

Claude can access the following tools to interact with WhatsApp:

- **search_contacts**: Search for contacts by name or phone number
- **list_messages**: Retrieve messages with optional filters and context
- **list_chats**: List available chats with metadata
- **get_chat**: Get information about a specific chat
- **get_direct_chat_by_contact**: Find a direct chat with a specific contact
- **get_contact_chats**: List all chats involving a specific contact
- **get_last_interaction**: Get the most recent message with a contact
- **get_message_context**: Retrieve context around a specific message
- **send_message**: Send a WhatsApp message to a specified phone number or group JID
- **send_file**: Send a file (image, video, raw audio, document) to a specified recipient
- **send_audio_message**: Send an audio file as a WhatsApp voice message (requires the file to be an .ogg opus file or ffmpeg must be installed)
- **download_media**: Download media from a WhatsApp message and get the local file path

### Media Handling Features

The MCP server supports both sending and receiving various media types:

#### Media Sending

You can send various media types to your WhatsApp contacts:

- **Images, Videos, Documents**: Use the `send_file` tool to share any supported media type.
- **Voice Messages**: Use the `send_audio_message` tool to send audio files as playable WhatsApp voice messages.
  - For optimal compatibility, audio files should be in `.ogg` Opus format.
  - With FFmpeg installed, the system will automatically convert other audio formats (MP3, WAV, etc.) to the required format.
  - Without FFmpeg, you can still send raw audio files using the `send_file` tool, but they won't appear as playable voice messages.

#### Media Downloading

By default, just the metadata of the media is stored in the local database. The message will indicate that media was sent. To access this media you need to use the download_media tool which takes the `message_id` and `chat_jid` (which are shown when printing messages containing the meda), this downloads the media and then returns the file path which can be then opened or passed to another tool.

## Technical Details

1. Claude sends requests to the Python MCP server
2. The MCP server queries the Go bridge for WhatsApp data or directly to the SQLite database
3. The Go accesses the WhatsApp API and keeps the SQLite database up to date
4. Data flows back through the chain to Claude
5. When sending messages, the request flows from Claude through the MCP server to the Go bridge and to WhatsApp

## Troubleshooting

- If you encounter permission issues when running uv, you may need to add it to your PATH or use the full path to the executable.
- Make sure both the Go application and the Python server are running for the integration to work properly.

### Authentication Issues

- **QR Code Not Displaying**: If the QR code doesn't appear, try restarting the authentication script. If issues persist, check if your terminal supports displaying QR codes.
- **WhatsApp Already Logged In**: If your session is already active, the Go bridge will automatically reconnect without showing a QR code.
- **Device Limit Reached**: WhatsApp limits the number of linked devices. If you reach this limit, you'll need to remove an existing device from WhatsApp on your phone (Settings > Linked Devices).
- **No Messages Loading**: After initial authentication, it can take several minutes for your message history to load, especially if you have many chats.
- **WhatsApp Out of Sync**: If your WhatsApp messages get out of sync with the bridge, delete both database files (`whatsapp-bridge/store/messages.db` and `whatsapp-bridge/store/whatsapp.db`) and restart the bridge to re-authenticate.

For additional Claude Desktop integration troubleshooting, see the [MCP documentation](https://modelcontextprotocol.io/quickstart/server#claude-for-desktop-integration-issues). The documentation includes helpful tips for checking logs and resolving common issues.
