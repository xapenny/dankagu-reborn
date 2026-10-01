"""HTML Portal and Certificate Delivery UI for Danmaku Kagura."""


def render_portal_html(
    lan_ip: str,
    http_port: int,
    lcx_port: int,
    grpc_port: int,
    public_host: str = "",
) -> str:
    """Render a standalone, self-contained, responsive HTML portal."""
    server_display = public_host or lan_ip
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Danmaku Kagura Preservation Server Portal</title>
  <style>
    :root {{
      --bg: #0f111a;
      --card-bg: #1a1d2d;
      --card-border: #2b3048;
      --text: #e2e8f0;
      --text-muted: #94a3b8;
      --primary: #8b5cf6;
      --primary-hover: #7c3aed;
      --primary-glow: rgba(139, 92, 246, 0.3);
      --success: #10b981;
      --success-glow: rgba(16, 185, 129, 0.2);
      --warning: #f59e0b;
      --accent: #ec4899;
    }}
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background-color: var(--bg);
      color: var(--text);
      line-height: 1.6;
      padding: 2rem 1rem;
      display: flex;
      justify-content: center;
      min-height: 100vh;
    }}
    .container {{
      max-width: 860px;
      width: 100%;
    }}
    header {{
      text-align: center;
      margin-bottom: 2.5rem;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid var(--success);
      color: #34d399;
      padding: 0.35rem 0.85rem;
      border-radius: 9999px;
      font-size: 0.875rem;
      font-weight: 600;
      margin-bottom: 1rem;
    }}
    .pulse {{
      width: 8px;
      height: 8px;
      background-color: var(--success);
      border-radius: 50%;
      box-shadow: 0 0 10px var(--success);
      animation: pulse 2s infinite;
    }}
    @keyframes pulse {{
      0% {{ transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }}
      70% {{ transform: scale(1); box-shadow: 0 0 0 10px rgba(16, 185, 129, 0); }}
      100% {{ transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }}
    }}
    h1 {{
      font-size: 2.25rem;
      font-weight: 800;
      background: linear-gradient(135deg, #c084fc, #f472b6, #fbbf24);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      margin-bottom: 0.5rem;
    }}
    p.subtitle {{
      color: var(--text-muted);
      font-size: 1.05rem;
    }}
    .card {{
      background-color: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 1rem;
      padding: 1.75rem;
      margin-bottom: 1.5rem;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3);
    }}
    .card h2 {{
      font-size: 1.35rem;
      margin-bottom: 1rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
      color: #fff;
    }}
    .download-section {{
      text-align: center;
      padding: 2.25rem 1.5rem;
      background: linear-gradient(145deg, #1e1b4b, #2e1065);
      border: 1px solid #4c1d95;
    }}
    .connection-section {{
      background: linear-gradient(145deg, #064e3b, #042f2e);
      border: 1px solid #059669;
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.75rem;
      padding: 1rem 2rem;
      border-radius: 0.75rem;
      font-size: 1.125rem;
      font-weight: 700;
      text-decoration: none;
      transition: all 0.2s ease;
      cursor: pointer;
    }}
    .btn-primary {{
      background: linear-gradient(135deg, #9333ea, #7928ca);
      color: white;
      box-shadow: 0 4px 20px var(--primary-glow);
    }}
    .btn-primary:hover {{
      background: linear-gradient(135deg, #a855f7, #9333ea);
      transform: translateY(-2px);
      box-shadow: 0 6px 25px rgba(168, 85, 247, 0.5);
    }}
    .btn-secondary {{
      background: rgba(255, 255, 255, 0.08);
      color: var(--text);
      border: 1px solid var(--card-border);
      font-size: 0.95rem;
      padding: 0.65rem 1.25rem;
      margin-top: 1rem;
    }}
    .btn-secondary:hover {{
      background: rgba(255, 255, 255, 0.15);
    }}
    .steps {{
      display: flex;
      flex-direction: column;
      gap: 1rem;
      margin-top: 1.25rem;
    }}
    .step {{
      display: flex;
      gap: 1rem;
      align-items: flex-start;
      background: rgba(0, 0, 0, 0.2);
      padding: 1rem;
      border-radius: 0.75rem;
      border: 1px solid rgba(255, 255, 255, 0.05);
    }}
    .step-number {{
      width: 2rem;
      height: 2rem;
      border-radius: 50%;
      background: var(--primary);
      color: white;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      flex-shrink: 0;
    }}
    .step-number.green {{
      background: #10b981;
    }}
    .step-content strong {{
      color: #fff;
      display: block;
      margin-bottom: 0.25rem;
    }}
    .step-content p {{
      color: var(--text-muted);
      font-size: 0.95rem;
    }}
    .highlight-box {{
      background: rgba(0, 0, 0, 0.35);
      border: 1px solid #10b981;
      padding: 1rem;
      border-radius: 0.5rem;
      margin-top: 0.75rem;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 1rem;
      color: #34d399;
    }}
    .grid-info {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 1rem;
      margin-top: 1rem;
    }}
    .info-box {{
      background: rgba(0, 0, 0, 0.25);
      padding: 1rem;
      border-radius: 0.75rem;
      border: 1px solid var(--card-border);
    }}
    .info-box .label {{
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
    }}
    .info-box .val {{
      font-size: 1.15rem;
      font-weight: 700;
      color: #38bdf8;
      margin-top: 0.25rem;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    .domains-list {{
      margin-top: 0.75rem;
      background: #090b10;
      padding: 0.75rem 1rem;
      border-radius: 0.5rem;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.875rem;
      color: #a5b4fc;
      line-height: 1.8;
    }}
    .lang-tabs {{
      display: flex;
      justify-content: center;
      gap: 0.5rem;
      margin-bottom: 1.5rem;
    }}
    .lang-btn {{
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid var(--card-border);
      color: var(--text-muted);
      padding: 0.4rem 1rem;
      border-radius: 9999px;
      font-size: 0.875rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
    }}
    .lang-btn.active {{
      background: var(--primary);
      color: #fff;
      border-color: var(--primary);
    }}
    footer {{
      text-align: center;
      color: var(--text-muted);
      font-size: 0.85rem;
      margin-top: 2rem;
      padding-top: 1rem;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="badge">
        <span class="pulse"></span>
        <span>Danmaku Kagura Preservation Server Online</span>
      </div>
      <h1>東方ダンマクカグラ</h1>
      <p class="subtitle">Touhou Danmaku Kagura Offline Backend & Certificate Portal</p>
    </header>

    <div class="lang-tabs">
      <button class="lang-btn active" onclick="switchLang('en')">English</button>
      <button class="lang-btn" onclick="switchLang('zh')">简体中文</button>
    </div>

    <!-- CA Certificate Download Card -->
    <div class="card download-section">
      <h2 style="justify-content: center;">🛡️ Root CA Certificate</h2>
      <p style="color: #cbd5e1; max-width: 540px; margin: 0 auto 1.5rem auto;">
        Install the preservation Root CA certificate onto your iOS or Android device to enable trusted HTTPS and gRPC communications with the server.
      </p>
      <div>
        <a href="/ca.crt" class="btn btn-primary">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
            <polyline points="7 10 12 15 17 10"></polyline>
            <line x1="12" y1="15" x2="12" y2="3"></line>
          </svg>
          <span class="lang-en">Download DanKagu Root CA</span>
          <span class="lang-zh" style="display:none;">一键安装 DanKagu 根证书</span>
        </a>
      </div>
      <p style="font-size: 0.85rem; color: #94a3b8; margin-top: 0.75rem;">
        <span class="lang-en">On iOS Safari: Tapping the button prompts native configuration profile installation.</span>
        <span class="lang-zh" style="display:none;">在 iOS Safari 浏览器中点击，系统将自动弹出描述文件安装提示。</span>
      </p>
      <div>
        <a href="/server.crt" class="btn btn-secondary">
          <span class="lang-en">Download Server Certificate (server.crt)</span>
          <span class="lang-zh" style="display:none;">下载服务端证书 (server.crt)</span>
        </a>
      </div>
    </div>

    <!-- Client Connection Card -->
    <div class="card connection-section">
      <h2>🚀 <span class="lang-en">Point the Client at This Server</span><span class="lang-zh" style="display:none;">让客户端连接到本服务器</span></h2>
      <p style="color: #d1fae5; font-size: 0.95rem;">
        <span class="lang-en">Configure your client build to use the endpoints below. No DNS or proxy interception is performed by this server.</span>
        <span class="lang-zh" style="display:none;">请在客户端配置中使用以下地址。本服务器不提供 DNS 或代理劫持。</span>
      </p>
      <div class="highlight-box">
        Server / 服务器: <strong>{server_display}</strong><br>
        LCX Auth / 鉴权: <strong>https://{server_display}:{lcx_port}</strong><br>
        Takasho gRPC: <strong>{server_display}:{grpc_port}</strong><br>
        Assets / 资源: <strong>http://{server_display}:{http_port}/assets</strong>
      </div>
    </div>

    <!-- iOS Step-by-Step Instructions -->
    <div class="card">
      <h2>📱 <span class="lang-en">Complete Setup Walkthrough</span><span class="lang-zh" style="display:none;">完整安装与连接指引</span></h2>
      <div class="steps">
        <div class="step">
          <div class="step-number">1</div>
          <div class="step-content">
            <strong>
              <span class="lang-en">Download Certificate in Safari</span>
              <span class="lang-zh" style="display:none;">在 Safari 中下载证书</span>
            </strong>
            <p>
              <span class="lang-en">Open this portal in <strong>Safari</strong> on your device, and tap <strong>Download DanKagu Root CA</strong> above. When prompted <em>"This website is trying to download a configuration profile. Do you want to allow this?"</em>, tap <strong>Allow</strong>.</span>
              <span class="lang-zh" style="display:none;">在手机的 <strong>Safari</strong> 浏览器中打开本页面，点击上方的 <strong>一键安装 DanKagu 根证书</strong>。当弹出 <em>“此网站正在尝试下载一个配置描述文件”</em> 时，点击 <strong>允许</strong>。</span>
            </p>
          </div>
        </div>

        <div class="step">
          <div class="step-number">2</div>
          <div class="step-content">
            <strong>
              <span class="lang-en">Install the Profile in Settings</span>
              <span class="lang-zh" style="display:none;">在系统设置中安装描述文件</span>
            </strong>
            <p>
              <span class="lang-en">Open iOS <strong>Settings</strong>. Near the top, tap <strong>Profile Downloaded</strong> (or go to <em>General &gt; VPN &amp; Device Management</em>). Select <strong>DanKagu Root CA</strong> and tap <strong>Install</strong>.</span>
              <span class="lang-zh" style="display:none;">打开 iOS <strong>设置</strong>，点击顶部的 <strong>已下载描述文件</strong>（或前往 <em>通用 &gt; VPN 与设备管理</em>），选择 <strong>DanKagu Root CA</strong> 并点击 <strong>安装</strong>。</span>
            </p>
          </div>
        </div>

        <div class="step">
          <div class="step-number">3</div>
          <div class="step-content">
            <strong>
              <span class="lang-en">Enable Full Trust for Root Certificate (Crucial!)</span>
              <span class="lang-zh" style="display:none;">开启根证书完全信任（核心关键步）</span>
            </strong>
            <p>
              <span class="lang-en">In iOS <strong>Settings</strong>, go to <strong>General</strong> &gt; <strong>About</strong> &gt; scroll down to <strong>Certificate Trust Settings</strong>. Under <em>"Enable full trust for root certificates"</em>, toggle ON <strong>DanKagu Root CA</strong>.</span>
              <span class="lang-zh" style="display:none;">在 iOS <strong>设置</strong> 中，前往 <strong>通用</strong> &gt; <strong>关于本机</strong> &gt; 滑动到底部进入 <strong>证书信任设置</strong>。在 <em>“针对根证书启用完全信任”</em> 下开启 <strong>DanKagu Root CA</strong>。</span>
            </p>
          </div>
        </div>

        <div class="step">
          <div class="step-number green">4</div>
          <div class="step-content">
            <strong>
              <span class="lang-en">Launch the Game</span>
              <span class="lang-zh" style="display:none;">启动游戏</span>
            </strong>
            <p>
              <span class="lang-en">Make sure your client is configured to reach <code>{server_display}</code> (LCX <code>{lcx_port}</code>, gRPC <code>{grpc_port}</code>), then launch Touhou Danmaku Kagura.</span>
              <span class="lang-zh" style="display:none;">确认客户端已指向 <code>{server_display}</code>（鉴权端口 <code>{lcx_port}</code>，gRPC 端口 <code>{grpc_port}</code>），然后启动《东方弹幕神乐》。</span>
            </p>
          </div>
        </div>
      </div>
    </div>

    <!-- Server & Network Topology -->
    <div class="card">
      <h2>🌐 <span class="lang-en">Network &amp; Server Details</span><span class="lang-zh" style="display:none;">服务器与网络配置</span></h2>
      <div class="grid-info">
        <div class="info-box">
          <div class="label"><span class="lang-en">Server LAN IP</span><span class="lang-zh" style="display:none;">服务端局域网 IP</span></div>
          <div class="val">{lan_ip}</div>
        </div>
        <div class="info-box">
          <div class="label"><span class="lang-en">LCX Auth (iOS)</span><span class="lang-zh" style="display:none;">LCX 鉴权 (iOS)</span></div>
          <div class="val">:{lcx_port}</div>
        </div>
        <div class="info-box">
          <div class="label"><span class="lang-en">Takasho gRPC</span><span class="lang-zh" style="display:none;">Takasho gRPC 端口</span></div>
          <div class="val">:{grpc_port}</div>
        </div>
      </div>

      <div style="margin-top: 1.25rem;">
        <strong style="font-size: 0.95rem; color: #cbd5e1;">
          <span class="lang-en">Endpoints served:</span>
          <span class="lang-zh" style="display:none;">本服务器提供的端点：</span>
        </strong>
        <div class="domains-list">
          LCX auth: https://{server_display}:{lcx_port}<br>
          Takasho gRPC: {server_display}:{grpc_port}<br>
          Assets: http://{server_display}:{http_port}/assets
        </div>
      </div>
    </div>

    <footer>
      Touhou Danmaku Kagura Offline Backend Preservation Project &bull; Powered by FastAPI, grpc.aio, and Astral uv.
    </footer>
  </div>

  <script>
    function switchLang(lang) {{
      const enElems = document.querySelectorAll('.lang-en');
      const zhElems = document.querySelectorAll('.lang-zh');
      const btns = document.querySelectorAll('.lang-btn');

      if (lang === 'zh') {{
        enElems.forEach(el => el.style.display = 'none');
        zhElems.forEach(el => el.style.display = '');
        btns[0].classList.remove('active');
        btns[1].classList.add('active');
      }} else {{
        enElems.forEach(el => el.style.display = '');
        zhElems.forEach(el => el.style.display = 'none');
        btns[0].classList.add('active');
        btns[1].classList.remove('active');
      }}
    }}
  </script>
</body>
</html>
"""
