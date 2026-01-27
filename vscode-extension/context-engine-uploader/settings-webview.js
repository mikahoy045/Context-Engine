/**
 * Settings Webview for Context-Engine.AI
 * A beautiful HTML-based settings UI similar to Augment's approach
 */
const vscode = require('vscode');

// Settings grouped by category
const SETTINGS_SCHEMA = {
  general: {
    title: 'General',
    icon: 'home',
    description: 'Core configuration for Context Engine',
    settings: [
      { key: 'runOnStartup', label: 'Run on Startup', type: 'boolean', description: 'Automatically start indexing when VS Code opens' },
      { key: 'endpoint', label: 'Server Endpoint', type: 'string', description: 'URL for the upload server', placeholder: 'http://localhost:8004' },
      { key: 'targetPath', label: 'Workspace Path', type: 'string', description: 'Path to index (leave empty for current workspace)', placeholder: '/path/to/project' },
      { key: 'pythonPath', label: 'Python Path', type: 'string', description: 'Python executable for scripts', placeholder: 'python3' },
    ]
  },
  indexing: {
    title: 'Indexing',
    icon: 'database',
    description: 'Watch mode and git history settings',
    settings: [
      { key: 'intervalSeconds', label: 'Watch Interval', type: 'number', description: 'Seconds between watch mode polls', min: 1 },
      { key: 'startWatchAfterForce', label: 'Auto-start Watch', type: 'boolean', description: 'Start watch mode after force sync' },
      { key: 'gitMaxCommits', label: 'Git Max Commits', type: 'number', description: 'Maximum git commits to index (0 to disable)', min: 0 },
      { key: 'gitSince', label: 'Git Since', type: 'string', description: 'Only index commits after this date', placeholder: '2 years ago' },
    ]
  },
  mcp: {
    title: 'MCP Integrations',
    icon: 'plug',
    description: 'Enable MCP config writing for AI assistants',
    settings: [
      { key: 'mcpClaudeEnabled', label: 'Claude Code', type: 'boolean', description: 'Write .mcp.json for Claude Code' },
      { key: 'mcpWindsurfEnabled', label: 'Windsurf', type: 'boolean', description: 'Write MCP config for Windsurf/Codeium' },
      { key: 'mcpAugmentEnabled', label: 'Augment Code', type: 'boolean', description: 'Write MCP config for Augment' },
      { key: 'mcpAntigravityEnabled', label: 'Antigravity', type: 'boolean', description: 'Write MCP config for Google Antigravity' },
      { key: 'mcpCursorEnabled', label: 'Cursor', type: 'boolean', description: 'Write MCP config for Cursor (~/.cursor/mcp.json)' },
      { key: 'autoWriteMcpConfigOnStartup', label: 'Auto-write on Startup', type: 'boolean', description: 'Automatically write MCP configs when extension activates' },
    ]
  },
  mcpServer: {
    title: 'MCP Server',
    icon: 'server',
    description: 'Server architecture and bridge settings',
    settings: [
      { key: 'mcpServerMode', label: 'Server Mode', type: 'enum', options: ['bridge', 'direct'], description: 'Single bridge or separate indexer/memory servers' },
      { key: 'mcpTransportMode', label: 'Transport', type: 'enum', options: ['http', 'sse-remote'], description: 'HTTP direct or SSE tunnel transport' },
      { key: 'autoStartMcpBridge', label: 'Auto-start Bridge', type: 'boolean', description: 'Automatically start the MCP bridge server' },
      { key: 'mcpBridgePort', label: 'Bridge Port', type: 'number', description: 'Port for the MCP bridge HTTP server' },
      { key: 'mcpIndexerUrl', label: 'Indexer URL', type: 'string', description: 'MCP server URL for Qdrant indexer', placeholder: 'http://localhost:8003/mcp' },
      { key: 'mcpMemoryUrl', label: 'Memory URL', type: 'string', description: 'MCP server URL for memory/search', placeholder: 'http://localhost:8002/mcp' },
    ]
  },
  decoder: {
    title: 'Decoder & AI',
    icon: 'sparkle',
    description: 'Configure Prompt+ and AI backend',
    settings: [
      { key: 'decoderRuntime', label: 'Runtime', type: 'enum', options: ['glm', 'llamacpp'], description: 'GLM cloud API or local llama.cpp' },
      { key: 'decoderUrl', label: 'Decoder URL', type: 'string', description: 'Local llama.cpp endpoint', placeholder: 'http://localhost:8081' },
      { key: 'useGpuDecoder', label: 'Use GPU', type: 'boolean', description: 'Prefer GPU decoder for Prompt+' },
      { key: 'glmApiKey', label: 'GLM API Key', type: 'password', description: 'API key for GLM cloud service' },
      { key: 'glmApiBase', label: 'GLM API Base', type: 'string', description: 'GLM API base URL' },
      { key: 'glmModel', label: 'GLM Model', type: 'string', description: 'GLM model name', placeholder: 'glm-4.6' },
    ]
  },
  hook: {
    title: 'Claude Hook',
    icon: 'terminal',
    description: 'Claude Code prompt enhancement hook',
    settings: [
      { key: 'claudeHookEnabled', label: 'Enable Hook', type: 'boolean', description: 'Write Claude hook to .claude/settings.local.json' },
      { key: 'surfaceQdrantCollectionHint', label: 'Collection Hint', type: 'boolean', description: 'Add Qdrant collection ID hint to enhanced prompts' },
      { key: 'ctxIndexerUrl', label: 'CTX Indexer URL', type: 'string', description: 'MCP indexer endpoint for ctx.py', placeholder: 'http://localhost:8003/mcp' },
      { key: 'scaffoldCtxConfig', label: 'Scaffold Config', type: 'boolean', description: 'Create ctx_config.json and .env automatically' },
    ]
  },
  advanced: {
    title: 'Advanced',
    icon: 'settings-gear',
    description: 'Developer settings and overrides',
    settings: [
      { key: 'scriptWorkingDirectory', label: 'Script Directory', type: 'string', description: 'Override folder for upload scripts' },
      { key: 'hostRoot', label: 'Host Root', type: 'string', description: 'Host path prefix for container rewrites' },
      { key: 'containerRoot', label: 'Container Root', type: 'string', description: 'Container path mirroring host root', placeholder: '/work' },
      { key: 'devRemoteMode', label: 'Dev Remote Mode', type: 'boolean', description: 'Enable dev-remote upload mode' },
      { key: 'mcpBridgeBinPath', label: 'Bridge Binary', type: 'string', description: 'Path to ctxce CLI binary' },
      { key: 'mcpBridgeLocalOnly', label: 'Local Bridge Only', type: 'boolean', description: 'Prefer local bridge binaries' },
    ]
  },
  paths: {
    title: 'Custom Paths',
    icon: 'folder',
    description: 'Override default config file locations',
    settings: [
      { key: 'windsurfMcpPath', label: 'Windsurf Config', type: 'string', description: 'Custom Windsurf mcp_config.json path' },
      { key: 'augmentMcpPath', label: 'Augment Config', type: 'string', description: 'Custom Augment settings.json path' },
      { key: 'antigravityMcpPath', label: 'Antigravity Config', type: 'string', description: 'Custom Antigravity mcp_config.json path' },
    ]
  }
};

class SettingsWebviewProvider {
  static viewType = 'contextEngineSettingsPanel';

  constructor(extensionUri, deps = {}) {
    this._extensionUri = extensionUri;
    this._panel = undefined;
    this._activeSection = 'general';
    this._pendingChanges = {};
    this._profiles = deps.profiles || null;
    this._getEffectiveConfig = deps.getEffectiveConfig || null;
  }

  _getActiveProfileInfo() {
    if (!this._profiles || typeof this._profiles.getActiveProfileSummary !== 'function') {
      return { id: undefined, name: undefined };
    }
    return this._profiles.getActiveProfileSummary();
  }

  async _updateProfileOverride(key, value) {
    if (!this._profiles || typeof this._profiles.updateActiveProfileOverride !== 'function') {
      return false;
    }
    return this._profiles.updateActiveProfileOverride(key, value);
  }

  openSettings() {
    const column = vscode.window.activeTextEditor
      ? vscode.window.activeTextEditor.viewColumn
      : undefined;

    if (this._panel) {
      this._panel.reveal(column);
      return;
    }

    this._panel = vscode.window.createWebviewPanel(
      SettingsWebviewProvider.viewType,
      'Context Engine Settings',
      column || vscode.ViewColumn.One,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [this._extensionUri],
      }
    );

    this._panel.webview.html = this._getHtmlContent(this._panel.webview);

    this._panel.onDidDispose(() => {
      this._panel = undefined;
    });

    this._panel.webview.onDidReceiveMessage(async (message) => {
      await this._handleMessage(message);
    });
  }

  async _handleMessage(message) {
    const cfg = vscode.workspace.getConfiguration('contextEngineUploader');
    const activeProfile = this._getActiveProfileInfo();
    const hasActiveProfile = !!activeProfile.id;

    switch (message.command) {
      case 'updateSetting':
        if (!this._pendingChanges) this._pendingChanges = {};
        this._pendingChanges[message.key] = message.value;
        this._updateSaveButtonState();
        break;
      case 'saveAllSettings':
        if (this._pendingChanges) {
          if (hasActiveProfile) {
            for (const [key, value] of Object.entries(this._pendingChanges)) {
              await this._updateProfileOverride(key, value);
            }
            this._pendingChanges = {};
            vscode.window.showInformationMessage(`Context Engine: Settings saved to profile "${activeProfile.name || activeProfile.id}".`);
          } else {
            for (const [key, value] of Object.entries(this._pendingChanges)) {
              await cfg.update(key, value, vscode.ConfigurationTarget.Global);
            }
            this._pendingChanges = {};
            vscode.window.showInformationMessage('Context Engine: Settings saved to global configuration.');
          }
          this.refresh();
        }
        break;
      case 'setSection':
        this._activeSection = message.section;
        this.refresh();
        break;
      case 'openVsCodeSettings':
        vscode.commands.executeCommand('workbench.action.openSettings', 'contextEngineUploader');
        break;
    }
  }

  _updateSaveButtonState() {
    if (this._panel) {
      const hasChanges = this._pendingChanges && Object.keys(this._pendingChanges).length > 0;
      this._panel.webview.postMessage({ command: 'updateSaveButton', hasChanges });
    }
  }

  refresh() {
    if (this._panel) {
      this._panel.webview.html = this._getHtmlContent(this._panel.webview);
      this._updateSaveButtonState();
    }
  }

  _getAllSettings() {
    const cfg = this._getEffectiveConfig
      ? this._getEffectiveConfig()
      : vscode.workspace.getConfiguration('contextEngineUploader');
    const values = {};
    for (const [categoryKey, category] of Object.entries(SETTINGS_SCHEMA)) {
      for (const setting of category.settings) {
        values[setting.key] = cfg.get(setting.key);
      }
    }
    return values;
  }

  _getHtmlContent(webview) {
    const nonce = getNonce();
    const values = this._getAllSettings();
    const logoUri = webview.asWebviewUri(vscode.Uri.joinPath(this._extensionUri, 'assets', 'logo.jpeg'));
    const activeProfile = this._getActiveProfileInfo();
    const profileBadge = activeProfile.id
      ? `<div class="profile-badge active"><span class="codicon codicon-account"></span> Profile: ${activeProfile.name || activeProfile.id}</div>`
      : `<div class="profile-badge global"><span class="codicon codicon-globe"></span> Global Settings</div>`;

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; font-src https://microsoft.github.io; img-src ${webview.cspSource}; script-src 'nonce-${nonce}' 'unsafe-inline';">
  <title>Context Engine Settings</title>
  <link href="https://microsoft.github.io/vscode-codicons/dist/codicon.css" rel="stylesheet">
  <style>${this._getStyles()}</style>
</head>
<body>
  <div class="settings-container">
    <aside class="sidebar">
      <div class="sidebar-header">
        <img src="${logoUri}" alt="Context Engine" class="logo-img">
        <h1>Settings</h1>
        ${profileBadge}
      </div>
      <nav class="nav-list">
        ${this._getNavItems()}
      </nav>
      <div class="sidebar-footer">
        <button class="save-btn ${this._pendingChanges && Object.keys(this._pendingChanges).length > 0 ? 'has-changes' : ''}" id="saveBtn" onclick="saveAllSettings()" ${this._pendingChanges && Object.keys(this._pendingChanges).length > 0 ? '' : 'disabled'}>
          <span class="codicon codicon-save"></span>
          ${activeProfile.id ? 'Save to Profile' : 'Save Settings'}
        </button>
        <button class="link-btn" onclick="openVsCodeSettings()">
          <span class="codicon codicon-json"></span>
          Edit JSON
        </button>
      </div>
    </aside>
    <main class="content">
      ${this._getSectionContent(values)}
    </main>
  </div>
  <script nonce="${nonce}">${this._getScript()}</script>
</body>
</html>`;
  }

  _getNavItems() {
    return Object.entries(SETTINGS_SCHEMA).map(([key, section]) => `
      <button class="nav-item ${this._activeSection === key ? 'active' : ''}" data-section="${key}">
        <span class="codicon codicon-${section.icon}"></span>
        <span>${section.title}</span>
      </button>
    `).join('');
  }

  _getSectionContent(values) {
    const section = SETTINGS_SCHEMA[this._activeSection];
    if (!section) return '';

    return `
      <div class="section-header">
        <h2><span class="codicon codicon-${section.icon}"></span> ${section.title}</h2>
        <p class="section-desc">${section.description}</p>
      </div>
      <div class="settings-list">
        ${section.settings.map(s => this._getSettingHtml(s, values[s.key])).join('')}
      </div>
    `;
  }

  _getSettingHtml(setting, value) {
    const id = `setting-${setting.key}`;
    let input = '';

    switch (setting.type) {
      case 'boolean':
        input = `
          <label class="toggle">
            <input type="checkbox" id="${id}" ${value ? 'checked' : ''} onchange="updateSetting('${setting.key}', this.checked)">
            <span class="toggle-slider"></span>
          </label>`;
        break;
      case 'enum':
        input = `
          <select id="${id}" onchange="updateSetting('${setting.key}', this.value)">
            ${setting.options.map(opt => `<option value="${opt}" ${value === opt ? 'selected' : ''}>${opt}</option>`).join('')}
          </select>`;
        break;
      case 'number':
        input = `<input type="number" id="${id}" value="${value ?? ''}" ${setting.min !== undefined ? `min="${setting.min}"` : ''} onchange="updateSetting('${setting.key}', parseInt(this.value, 10))" placeholder="${setting.placeholder || ''}">`;
        break;
      case 'password':
        input = `<input type="password" id="${id}" value="${value || ''}" oninput="updateSetting('${setting.key}', this.value)" placeholder="••••••••">`;
        break;
      default:
        input = `<input type="text" id="${id}" value="${value || ''}" oninput="updateSetting('${setting.key}', this.value)" placeholder="${setting.placeholder || ''}">`;
    }

    return `
      <div class="setting-row">
        <div class="setting-info">
          <label for="${id}" class="setting-label">${setting.label}</label>
          <p class="setting-desc">${setting.description}</p>
        </div>
        <div class="setting-control">${input}</div>
      </div>`;
  }

  _getStyles() {
    return `
    :root {
      --bg-base: var(--vscode-editor-background);
      --bg-subtle: var(--vscode-sideBar-background);
      --bg-card: var(--vscode-editorWidget-background);
      --bg-hover: color-mix(in srgb, var(--vscode-list-hoverBackground) 80%, transparent);
      --text-primary: var(--vscode-foreground);
      --text-secondary: var(--vscode-descriptionForeground);
      --text-muted: color-mix(in srgb, var(--text-secondary) 60%, transparent);
      --accent: var(--vscode-button-background);
      --accent-hover: var(--vscode-button-hoverBackground);
      --border: var(--vscode-panel-border);
      --border-subtle: color-mix(in srgb, var(--border) 40%, transparent);
      --input-bg: var(--vscode-input-background);
      --input-border: var(--vscode-input-border);
      --focus-ring: var(--vscode-focusBorder);
      --radius-sm: 4px;
      --radius-md: 6px;
      --radius-lg: 8px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--vscode-font-family);
      font-size: 13px;
      color: var(--text-primary);
      background: var(--bg-base);
      line-height: 1.5;
      height: 100vh;
      overflow: hidden;
    }
    .settings-container {
      display: grid;
      grid-template-columns: 220px 1fr;
      height: 100vh;
    }
    .sidebar {
      background: var(--bg-subtle);
      border-right: 1px solid var(--border-subtle);
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .sidebar-header {
      padding: 16px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
      border-bottom: 1px solid var(--border-subtle);
    }
    .sidebar-header .logo-img {
      width: 28px;
      height: 28px;
      border-radius: var(--radius-md);
      object-fit: cover;
    }
    .sidebar-header h1 {
      font-size: 14px;
      font-weight: 600;
    }
    .profile-badge {
      width: 100%;
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border-radius: var(--radius-md);
      font-size: 11px;
      font-weight: 500;
      margin-top: 4px;
    }
    .profile-badge.active {
      background: linear-gradient(135deg, rgba(79, 192, 141, 0.15), rgba(79, 192, 141, 0.05));
      border: 1px solid rgba(79, 192, 141, 0.3);
      color: rgb(79, 192, 141);
    }
    .profile-badge.global {
      background: linear-gradient(135deg, rgba(100, 149, 237, 0.15), rgba(100, 149, 237, 0.05));
      border: 1px solid rgba(100, 149, 237, 0.3);
      color: rgb(100, 149, 237);
    }
    .profile-badge .codicon {
      font-size: 12px;
    }
    .nav-list {
      flex: 1;
      overflow-y: auto;
      padding: 8px;
    }
    .nav-item {
      width: 100%;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      background: transparent;
      border: none;
      border-radius: var(--radius-md);
      color: var(--text-secondary);
      cursor: pointer;
      font-size: 13px;
      text-align: left;
      transition: all 0.15s ease;
    }
    .nav-item:hover {
      background: var(--bg-hover);
      color: var(--text-primary);
    }
    .nav-item.active {
      background: color-mix(in srgb, var(--accent) 15%, transparent);
      color: var(--accent);
      font-weight: 500;
    }
    .nav-item .codicon { font-size: 16px; opacity: 0.8; }
    .sidebar-footer {
      padding: 12px;
      border-top: 1px solid var(--border-subtle);
    }
    .save-btn {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      width: 100%;
      padding: 12px 16px;
      margin-bottom: 8px;
      background: var(--accent);
      border: none;
      border-radius: var(--radius-sm);
      color: white;
      cursor: pointer;
      font-size: 13px;
      font-weight: 500;
      transition: all 0.15s ease;
      opacity: 0.5;
    }
    .save-btn:disabled { cursor: not-allowed; opacity: 0.4; }
    .save-btn.has-changes { opacity: 1; animation: pulse 1.5s infinite; }
    .save-btn:not(:disabled):hover { background: var(--accent-hover); }
    @keyframes pulse {
      0%, 100% { box-shadow: 0 0 0 0 rgba(var(--accent), 0.4); }
      50% { box-shadow: 0 0 0 4px rgba(0, 120, 212, 0.2); }
    }
    .link-btn {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 10px 16px;
      background: transparent;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      color: var(--text-secondary);
      cursor: pointer;
      font-size: 13px;
      transition: all 0.15s ease;
    }
    .link-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
    .content {
      overflow-y: auto;
      padding: 32px 40px;
    }
    .section-header { margin-bottom: 24px; }
    .section-header h2 {
      font-size: 18px;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 8px;
    }
    .section-header h2 .codicon { color: var(--accent); }
    .section-desc { color: var(--text-secondary); font-size: 13px; }
    .settings-list { display: flex; flex-direction: column; gap: 2px; }
    .setting-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 16px;
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-md);
      margin-bottom: 8px;
      transition: border-color 0.15s ease;
    }
    .setting-row:hover { border-color: var(--border); }
    .setting-info { flex: 1; min-width: 0; padding-right: 24px; }
    .setting-label { font-weight: 500; display: block; margin-bottom: 4px; }
    .setting-desc { color: var(--text-secondary); font-size: 12px; }
    .setting-control { flex-shrink: 0; }
    input[type="text"], input[type="password"], input[type="number"], select {
      min-width: 200px;
      padding: 8px 12px;
      background: var(--input-bg);
      border: 1px solid var(--input-border);
      border-radius: var(--radius-sm);
      color: var(--text-primary);
      font-size: 13px;
      transition: border-color 0.15s ease;
    }
    input:focus, select:focus { outline: none; border-color: var(--focus-ring); }
    select { cursor: pointer; }
    .toggle { position: relative; display: inline-block; width: 40px; height: 22px; }
    .toggle input { opacity: 0; width: 0; height: 0; }
    .toggle-slider {
      position: absolute;
      cursor: pointer;
      inset: 0;
      background: var(--border);
      border-radius: 22px;
      transition: 0.2s ease;
    }
    .toggle-slider::before {
      content: '';
      position: absolute;
      width: 16px;
      height: 16px;
      left: 3px;
      bottom: 3px;
      background: white;
      border-radius: 50%;
      transition: 0.2s ease;
    }
    .toggle input:checked + .toggle-slider { background: var(--accent); }
    .toggle input:checked + .toggle-slider::before { transform: translateX(18px); }
    .toggle input:focus + .toggle-slider { box-shadow: 0 0 0 2px var(--focus-ring); }
    `;
  }

  _getScript() {
    return `
    const vscode = acquireVsCodeApi();
    let pendingCount = 0;
    function updateSetting(key, value) {
      vscode.postMessage({ command: 'updateSetting', key, value });
      pendingCount++;
      updateSaveButtonUI(true);
    }
    function saveAllSettings() {
      vscode.postMessage({ command: 'saveAllSettings' });
      pendingCount = 0;
      updateSaveButtonUI(false);
    }
    function openVsCodeSettings() {
      vscode.postMessage({ command: 'openVsCodeSettings' });
    }
    function updateSaveButtonUI(hasChanges) {
      const btn = document.getElementById('saveBtn');
      if (btn) {
        btn.disabled = !hasChanges;
        btn.classList.toggle('has-changes', hasChanges);
      }
    }
    window.addEventListener('message', event => {
      const msg = event.data;
      if (msg.command === 'updateSaveButton') {
        pendingCount = msg.hasChanges ? 1 : 0;
        updateSaveButtonUI(msg.hasChanges);
      }
    });
    document.querySelectorAll('.nav-item').forEach(btn => {
      btn.addEventListener('click', () => {
        vscode.postMessage({ command: 'setSection', section: btn.dataset.section });
      });
    });
    `;
  }
}

function getNonce() {
  let text = '';
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  for (let i = 0; i < 32; i++) {
    text += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return text;
}

module.exports = { SettingsWebviewProvider, SETTINGS_SCHEMA };
