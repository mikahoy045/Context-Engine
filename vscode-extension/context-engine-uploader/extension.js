const vscode = require('vscode');
const { spawn, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');
let outputChannel;
let watchProcess;
let forceProcess;
let extensionRoot;
let statusBarItem;
let promptStatusBarItem;
let logsTerminal;
let logTailActive = false;
let statusMode = 'idle';
let workspaceWatcher;
let watchedTargetPath;
let indexedWatchDisposables = [];
let globalStoragePath;
let pythonOverridePath;
const REQUIRED_PYTHON_MODULES = ['requests', 'urllib3', 'charset_normalizer'];
const DEFAULT_CONTAINER_ROOT = '/work';
// const CLAUDE_HOOK_COMMAND = '/home/coder/project/Context-Engine/ctx-hook-simple.sh';
function activate(context) {
  outputChannel = vscode.window.createOutputChannel('Context Engine Upload');
  context.subscriptions.push(outputChannel);
  extensionRoot = context.extensionPath;
  globalStoragePath = context.globalStorageUri && context.globalStorageUri.fsPath ? context.globalStorageUri.fsPath : undefined;
  try {
    const venvPy = resolvePrivateVenvPython();
    if (venvPy) {
      pythonOverridePath = venvPy;
      log(`Detected existing private venv interpreter: ${venvPy}`);
    }
  } catch (_) {}
  statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBarItem.command = 'contextEngineUploader.indexCodebase';
  context.subscriptions.push(statusBarItem);
  statusBarItem.show();
  setStatusBarState('idle');
  updateStatusBarTooltip();
  promptStatusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 90);
  promptStatusBarItem.command = 'contextEngineUploader.promptEnhance';
  promptStatusBarItem.text = '$(sparkle) Prompt+';
  promptStatusBarItem.tooltip = 'Enhance selection with Unicorn Mode via ctx.py';
  context.subscriptions.push(promptStatusBarItem);
  promptStatusBarItem.show();

  // Ensure an output channel is visible early for user feedback
  if (outputChannel) { outputChannel.show(true); }
  const startDisposable = vscode.commands.registerCommand('contextEngineUploader.start', () => {
    runSequence('auto').catch(error => log(`Start failed: ${error instanceof Error ? error.message : String(error)}`));
  });
  const stopDisposable = vscode.commands.registerCommand('contextEngineUploader.stop', () => {
    stopProcesses().catch(error => log(`Stop failed: ${error instanceof Error ? error.message : String(error)}`));
  });
  const restartDisposable = vscode.commands.registerCommand('contextEngineUploader.restart', () => {
    stopProcesses().then(() => runSequence('auto')).catch(error => log(`Restart failed: ${error instanceof Error ? error.message : String(error)}`));
  });
  const indexDisposable = vscode.commands.registerCommand('contextEngineUploader.indexCodebase', () => {
    vscode.window.showInformationMessage('Context Engine indexing started.');
    if (outputChannel) { outputChannel.show(true); }
    try {
      const cfg = vscode.workspace.getConfiguration('contextEngineUploader');
      if (cfg.get('autoTailUploadLogs', true)) {
        openUploadServiceLogsTerminal();
      }
    } catch (e) {
      log(`Auto-tail logs failed: ${e && e.message ? e.message : String(e)}`);
    }
    runSequence('force').catch(error => log(`Index failed: ${error instanceof Error ? error.message : String(error)}`));
  });
  const ctxConfigDisposable = vscode.commands.registerCommand('contextEngineUploader.writeCtxConfig', () => {
    writeCtxConfig().catch(error => log(`CTX config write failed: ${error instanceof Error ? error.message : String(error)}`));
  });
  const mcpConfigDisposable = vscode.commands.registerCommand('contextEngineUploader.writeMcpConfig', () => {
    writeMcpConfig().catch(error => log(`MCP config write failed: ${error instanceof Error ? error.message : String(error)}`));
  });
  const showLogsDisposable = vscode.commands.registerCommand('contextEngineUploader.showUploadServiceLogs', () => {
    try { openUploadServiceLogsTerminal(); } catch (e) { log(`Show logs failed: ${e && e.message ? e.message : String(e)}`); }
  });
  const promptEnhanceDisposable = vscode.commands.registerCommand('contextEngineUploader.promptEnhance', () => {
    enhanceSelectionWithUnicorn().catch(error => {
      log(`Prompt+ failed: ${error instanceof Error ? error.message : String(error)}`);
      vscode.window.showErrorMessage('Prompt+ failed. See Context Engine Upload output.');
    });
  });
  const configDisposable = vscode.workspace.onDidChangeConfiguration(event => {
    if (event.affectsConfiguration('contextEngineUploader') && watchProcess) {
      runSequence('auto').catch(error => log(`Auto-restart failed: ${error instanceof Error ? error.message : String(error)}`));
    }
    if (event.affectsConfiguration('contextEngineUploader.targetPath')) {
      updateStatusBarTooltip();
    }
    if (
      event.affectsConfiguration('contextEngineUploader.mcpIndexerUrl') ||
      event.affectsConfiguration('contextEngineUploader.mcpMemoryUrl') ||
      event.affectsConfiguration('contextEngineUploader.mcpClaudeEnabled') ||
      event.affectsConfiguration('contextEngineUploader.mcpWindsurfEnabled') ||
      event.affectsConfiguration('contextEngineUploader.windsurfMcpPath') ||
      event.affectsConfiguration('contextEngineUploader.claudeHookEnabled') ||
      event.affectsConfiguration('contextEngineUploader.surfaceQdrantCollectionHint')
    ) {
      // Best-effort auto-update of MCP + hook configurations when settings change
      writeMcpConfig().catch(error => log(`Auto MCP config write failed: ${error instanceof Error ? error.message : String(error)}`));
    }
  });
  const workspaceDisposable = vscode.workspace.onDidChangeWorkspaceFolders(() => {
    ensureTargetPathConfigured();
  });
  const terminalCloseDisposable = vscode.window.onDidCloseTerminal(term => {
    if (term === logsTerminal) {
      logsTerminal = undefined;
      logTailActive = false;
    }
  });
  context.subscriptions.push(
    startDisposable,
    stopDisposable,
    restartDisposable,
    indexDisposable,
    showLogsDisposable,
    promptEnhanceDisposable,
    mcpConfigDisposable,
    ctxConfigDisposable,
    configDisposable,
    workspaceDisposable,
    terminalCloseDisposable
  );
  const config = vscode.workspace.getConfiguration('contextEngineUploader');
  ensureTargetPathConfigured();
  if (config.get('runOnStartup')) {
    runSequence('auto').catch(error => log(`Startup run failed: ${error instanceof Error ? error.message : String(error)}`));
  }

  // When enabled, best-effort auto-scaffold ctx_config.json/.env for the current targetPath on activation
  if (config.get('scaffoldCtxConfig', true)) {
    writeCtxConfig().catch(error => log(`CTX config auto-scaffold on activation failed: ${error instanceof Error ? error.message : String(error)}`));
  }
}
async function runSequence(mode = 'auto') {
  const options = resolveOptions();
  if (!options) {
    return;
  }
  const depsSatisfied = await ensurePythonDependencies(options.pythonPath);
  if (!depsSatisfied) {
    setStatusBarState('idle');
    return;
  }
  // Re-resolve options in case ensurePythonDependencies switched to a private venv interpreter
  const reoptions = resolveOptions();
  if (reoptions) {
    Object.assign(options, reoptions);
  }
  await stopProcesses();
  const needsForce = mode === 'force' || needsForceSync(options.targetPath);
  if (needsForce) {
    setStatusBarState('indexing');
    if (outputChannel) { outputChannel.show(true); }
    const code = await runOnce(options);
    if (code === 0) {
      setStatusBarState('indexed');
      ensureIndexedWatcher(options.targetPath);
      if (options.startWatchAfterForce) {
        startWatch(options);
      }
    } else {
      setStatusBarState('idle');
    }
    return;
  }
  startWatch(options);
}
function resolveOptions() {
  const config = vscode.workspace.getConfiguration('contextEngineUploader');
  let pythonPath = (config.get('pythonPath') || 'python3').trim();
  if (pythonOverridePath && fs.existsSync(pythonOverridePath)) {
    pythonPath = pythonOverridePath;
  }
  const endpoint = (config.get('endpoint') || '').trim();
  const targetPath = getTargetPath(config);
  const interval = config.get('intervalSeconds') || 5;
  const extraForceArgs = config.get('extraForceArgs') || [];
  const extraWatchArgs = config.get('extraWatchArgs') || [];
  const hostRootOverride = (config.get('hostRoot') || '').trim();
  const containerRoot = (config.get('containerRoot') || DEFAULT_CONTAINER_ROOT).trim() || DEFAULT_CONTAINER_ROOT;
  const startWatchAfterForce = config.get('startWatchAfterForce', true);
  const configuredScriptDir = (config.get('scriptWorkingDirectory') || '').trim();
  const candidates = [];
  if (configuredScriptDir) {
    candidates.push(configuredScriptDir);
  }
  // Prefer packaged script; also try workspace ./scripts fallback for dev
  candidates.push(extensionRoot);
  const wsRoot = getWorkspaceFolderPath();
  if (wsRoot) {
    candidates.push(path.join(wsRoot, 'scripts'));
  }
  candidates.push(path.join(extensionRoot, '..', 'out'));
  let workingDirectory;
  let scriptPath;
  for (const candidate of candidates) {
    if (!candidate) {
      continue;
    }
    const resolved = path.resolve(candidate);
    const testPath = path.join(resolved, 'standalone_upload_client.py');
    if (fs.existsSync(testPath)) {
      workingDirectory = resolved;
      scriptPath = testPath;
      break;
    }
  }
  if (!workingDirectory || !scriptPath) {
    vscode.window.showErrorMessage('Context Engine Uploader: extension path unavailable.');
    return undefined;
  }
  const scriptSource = workingDirectory === extensionRoot ? 'packaged' : (workingDirectory.includes('\\out') || workingDirectory.endsWith('/out') ? 'staged out' : 'custom');
  if (!endpoint) {
    vscode.window.showErrorMessage('Context Engine Uploader: set contextEngineUploader.endpoint.');
    return undefined;
  }
  if (!targetPath) {
    return undefined;
  }
  const resolvedTarget = path.resolve(targetPath);
  let derivedHostRoot = path.dirname(resolvedTarget);
  if (!derivedHostRoot || derivedHostRoot === resolvedTarget) {
    derivedHostRoot = resolvedTarget;
  }
  const hostRoot = hostRootOverride || derivedHostRoot;
  log(`Using ${scriptSource} standalone_upload_client.py at ${scriptPath}`);
  log(`Uploader path mapping hostRoot=${hostRoot || 'n/a'} -> containerRoot=${containerRoot}`);
  return {
    pythonPath,
    workingDirectory,
    scriptPath,
    targetPath,
    endpoint,
    interval,
    extraForceArgs,
    extraWatchArgs,
    hostRoot,
    containerRoot,
    startWatchAfterForce
  };
}
function getTargetPath(config) {
  let inspected;
  try {
    if (typeof config.inspect === 'function') {
      inspected = config.inspect('targetPath');
    }
  } catch (error) {
    inspected = undefined;
  }
  let targetPath = (config.get('targetPath') || '').trim();
  if (inspected && targetPath) {
    let sourceLabel = 'default';
    if (inspected.workspaceFolderValue !== undefined) {
      sourceLabel = 'workspaceFolder';
    } else if (inspected.workspaceValue !== undefined) {
      sourceLabel = 'workspace';
    } else if (inspected.globalValue !== undefined) {
      sourceLabel = 'user';
    }
    log(`Target path resolved to ${targetPath} (source: ${sourceLabel} settings)`);
    if (inspected.globalValue !== undefined && inspected.workspaceValue !== undefined && inspected.globalValue !== inspected.workspaceValue) {
      log('Target path has different user and workspace values; using workspace value. Update workspace settings (e.g. .vscode/settings.json) to change it.');
    }
  }
  if (targetPath) {
    updateStatusBarTooltip(targetPath);
    return targetPath;
  }
  const folderPath = getWorkspaceFolderPath();
  if (!folderPath) {
    vscode.window.showErrorMessage('Context Engine Uploader: open a folder or set contextEngineUploader.targetPath.');
    updateStatusBarTooltip();
    return undefined;
  }
  updateStatusBarTooltip(folderPath);
  return folderPath;
}
function saveTargetPath(config, targetPath) {
  const hasWorkspace = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length;
  const target = hasWorkspace ? vscode.ConfigurationTarget.Workspace : vscode.ConfigurationTarget.Global;
  config.update('targetPath', targetPath, target).catch(error => {
    log(`Target path save failed: ${error instanceof Error ? error.message : String(error)}`);
  });
}
function getWorkspaceFolderPath() {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || !folders.length) {
    return undefined;
  }
  return folders[0].uri.fsPath;
}
function ensureTargetPathConfigured() {
  const config = vscode.workspace.getConfiguration('contextEngineUploader');
  const current = (config.get('targetPath') || '').trim();
  if (current) {
    updateStatusBarTooltip(current);
    return;
  }
  const folderPath = getWorkspaceFolderPath();
  if (!folderPath) {
    updateStatusBarTooltip();
    return;
  }
  updateStatusBarTooltip(folderPath);
}
function updateStatusBarTooltip(targetPath) {
  if (!statusBarItem) {
    return;
  }
  if (targetPath) {
    statusBarItem.tooltip = `Index Codebase (${targetPath})`;
  } else {
    statusBarItem.tooltip = 'Index Codebase';
  }
}
function needsForceSync(targetPath) {
  try {
    const cachePath = path.join(targetPath, '.context-engine', 'file_cache.json');
    if (!fs.existsSync(cachePath)) {
      return true;
    }
    const stats = fs.statSync(cachePath);
    return stats.size === 0;
  } catch (error) {
    log(`Force detection failed: ${error instanceof Error ? error.message : String(error)}`);
    return true;
  }
}
async function ensurePythonDependencies(pythonPath) {
  // Probe current interpreter; if modules missing, offer to create a private venv and install deps
  const ok = await checkPythonDeps(pythonPath);
  if (ok) return true;
  const choice = await vscode.window.showErrorMessage(
    'Context Engine Uploader: missing Python modules. Create isolated environment and auto-install?',
    'Auto-install to private venv',
    'Cancel'
  );
  if (choice !== 'Auto-install to private venv') {
    return false;
  }
  const created = await ensurePrivateVenv();
  if (!created) return false;
  const venvPython = resolvePrivateVenvPython();
  if (!venvPython) {
    vscode.window.showErrorMessage('Context Engine Uploader: failed to locate private venv python.');
    return false;
  }
  const installed = await installDepsInto(venvPython);
  if (!installed) return false;
  pythonOverridePath = venvPython;
  log(`Using private venv interpreter: ${pythonOverridePath}`);
  return await checkPythonDeps(pythonOverridePath);
}

async function checkPythonDeps(pythonPath) {
  const missing = [];
  let pythonError;
  const env = { ...process.env };
  try {
    const libsPath = path.join(extensionRoot, 'python_libs');
    if (fs.existsSync(libsPath)) {
      const existing = env.PYTHONPATH || '';
      env.PYTHONPATH = existing ? `${libsPath}${path.delimiter}${existing}` : libsPath;
      log(`Using bundled python_libs at ${libsPath} for dependency check.`);
    }
  } catch (error) {
    log(`Failed to configure PYTHONPATH for dependency check: ${error instanceof Error ? error.message : String(error)}`);
  }
  for (const moduleName of REQUIRED_PYTHON_MODULES) {
    const check = spawnSync(pythonPath, ['-c', `import ${moduleName}`], { encoding: 'utf8', env });
    if (check.error) {
      pythonError = check.error;
      break;
    }
    if (check.status !== 0) {
      missing.push(moduleName);
    }
  }
  if (pythonError) {
    const message = `Context Engine Uploader: failed to run ${pythonPath}. Update contextEngineUploader.pythonPath.`;
    vscode.window.showErrorMessage(message);
    log(`Dependency check failed: ${pythonError.message || pythonError}`);
    return false;
  }
  if (missing.length) {
    log(`Missing Python modules for ${pythonPath}: ${missing.join(', ')}`);
    return false;
  }
  return true;
}

function venvRootDir() {
  // Prefer workspace storage; fallback to extension storage
  try {
    const ws = getWorkspaceFolderPath();
    const base = ws && fs.existsSync(ws) ? path.join(ws, '.vscode', '.context-engine-uploader')
      : (globalStoragePath || path.join(extensionRoot, '.storage'));
    if (!fs.existsSync(base)) fs.mkdirSync(base, { recursive: true });
    return base;
  } catch (e) {
    return extensionRoot;
  }
}

function privateVenvPath() {
  return path.join(venvRootDir(), 'py-venv');
}

function resolvePrivateVenvPython() {
  const venvPath = privateVenvPath();
  const bin = process.platform === 'win32' ? path.join(venvPath, 'Scripts', 'python.exe') : path.join(venvPath, 'bin', 'python');
  return fs.existsSync(bin) ? bin : undefined;
}

async function ensurePrivateVenv() {
  try {
    const python = resolvePrivateVenvPython();
    if (python) {
      log('Private venv already exists.');
      return true;
    }
    const venvPath = privateVenvPath();
    const basePy = await detectSystemPython();
    if (!basePy) {
      vscode.window.showErrorMessage('Context Engine Uploader: no Python interpreter found to bootstrap venv.');
      return false;
    }
    log(`Creating private venv at ${venvPath} using ${basePy}`);
    const res = spawnSync(basePy, ['-m', 'venv', venvPath], { encoding: 'utf8' });
    if (res.status !== 0) {
      log(`venv creation failed: ${res.stderr || res.stdout}`);
      vscode.window.showErrorMessage('Context Engine Uploader: failed to create private venv.');
      return false;
    }
    return true;
  } catch (e) {
    log(`ensurePrivateVenv error: ${e && e.message ? e.message : String(e)}`);
    return false;
  }
}

async function installDepsInto(pythonBin) {
  try {
    log(`Installing Python deps into private venv via ${pythonBin}`);
    const args = ['-m', 'pip', 'install', ...REQUIRED_PYTHON_MODULES];
    const res = spawnSync(pythonBin, args, { encoding: 'utf8' });
    if (res.status !== 0) {
      log(`pip install failed: ${res.stderr || res.stdout}`);
      vscode.window.showErrorMessage('Context Engine Uploader: pip install failed. See Output for details.');
      return false;
    }
    return true;
  } catch (e) {
    log(`installDepsInto error: ${e && e.message ? e.message : String(e)}`);
    return false;
  }
}

async function detectSystemPython() {
  // Try configured pythonPath, then common names
  const candidates = [];
  try {
    const cfg = vscode.workspace.getConfiguration('contextEngineUploader');
    const configured = (cfg.get('pythonPath') || '').trim();
    if (configured) candidates.push(configured);
  } catch {}
  if (process.platform === 'win32') {
    candidates.push('py', 'python3', 'python');
  } else {
    candidates.push('python3', 'python');
    // Add common Homebrew path on Apple Silicon
    candidates.push('/opt/homebrew/bin/python3');
  }
  for (const cmd of candidates) {
    const probe = spawnSync(cmd, ['-c', 'import sys; print(sys.executable)'], { encoding: 'utf8' });
    if (!probe.error && probe.status === 0) {
      const p = (probe.stdout || '').trim();
      if (p) return p;
    }
  }
  return undefined;
}
function setStatusBarState(mode) {
  if (!statusBarItem) {
    return;
  }
  statusMode = mode;
  if (mode === 'indexing') {
    statusBarItem.text = '$(sync~spin) Indexing...';
    statusBarItem.color = undefined;
  } else if (mode === 'indexed') {
    statusBarItem.text = '$(check) Indexed';
    statusBarItem.color = new vscode.ThemeColor('charts.green');
  } else if (mode === 'watch') {
    statusBarItem.text = '$(sync) Watching (Click Force Index)';
    statusBarItem.color = new vscode.ThemeColor('charts.purple');
  } else {
    statusBarItem.text = '$(sync) Index Codebase';
    statusBarItem.color = undefined;
  }
}
function runOnce(options) {
  return new Promise(resolve => {
    const args = buildArgs(options, 'force');
    const child = spawn(options.pythonPath, args, { cwd: options.workingDirectory, env: buildChildEnv(options) });
    forceProcess = child;
    attachOutput(child, 'force');
    let finished = false;
    const finish = code => {
      if (finished) {
        return;
      }
      finished = true;
      log(`Force sync exited with code ${code}`);
      if (forceProcess === child) {
        forceProcess = undefined;
      }
      resolve(typeof code === 'number' ? code : 1);
    };
    child.on('close', finish);
    child.on('error', error => {
      log(`Force sync failed: ${error.message}`);
      finish(1);
    });
  });
}
function startWatch(options) {
  disposeIndexedWatcher();
  const args = buildArgs(options, 'watch');
  const child = spawn(options.pythonPath, args, { cwd: options.workingDirectory, env: buildChildEnv(options) });
  watchProcess = child;
  attachOutput(child, 'watch');
  if (outputChannel) { outputChannel.show(true); }
  setStatusBarState('watch');
  child.on('close', code => {
    log(`Watch exited with code ${code}`);
    if (watchProcess === child) {
      watchProcess = undefined;
      if (statusMode !== 'indexing') {
        setStatusBarState('idle');
      }
    }
  });
  child.on('error', error => {
    log(`Watch failed: ${error.message}`);
    if (watchProcess === child) {
      watchProcess = undefined;
      if (statusMode !== 'indexing') {
        setStatusBarState('idle');
      }
    }
  });
  vscode.window.showInformationMessage('Context Engine remote watch started.');
}
function buildArgs(options, mode) {
  const args = ['-u', options.scriptPath, '--path', options.targetPath, '--endpoint', options.endpoint];
  if (mode === 'force') {
    args.push('--force');
    if (options.extraForceArgs && options.extraForceArgs.length) {
      args.push(...options.extraForceArgs);
    }
  } else {
    args.push('--watch', '--interval', String(options.interval));
    if (options.extraWatchArgs && options.extraWatchArgs.length) {
      args.push(...options.extraWatchArgs);
    }
  }
  return args;
}
function attachOutput(child, label) {
  if (!outputChannel) {
    return;
  }
  if (child.stdout) {
    child.stdout.on('data', data => {
      outputChannel.append(`[${label}] ${data}`);
    });
  }
  if (child.stderr) {
    child.stderr.on('data', data => {
      outputChannel.append(`[${label} err] ${data}`);
    });
  }
}
function openUploadServiceLogsTerminal() {
  try {
    const cfg = vscode.workspace.getConfiguration('contextEngineUploader');
    const wsPath = getWorkspaceFolderPath() || (cfg.get('targetPath') || '');
    const cwd = (wsPath && typeof wsPath === 'string' && fs.existsSync(wsPath)) ? wsPath : undefined;
    if (logsTerminal && logsTerminal.exitStatus) {
      logsTerminal = undefined;
      logTailActive = false;
    }
    if (!logsTerminal) {
      logsTerminal = vscode.window.createTerminal({ name: 'Context Engine Upload Logs', cwd: cwd ? vscode.Uri.file(cwd) : undefined });
      logTailActive = false;
    }
    logsTerminal.show(true);
    if (!logTailActive) {
      logsTerminal.sendText('docker compose logs -f upload_service', true);
      logTailActive = true;
    }
  } catch (e) {
    log(`Unable to open logs terminal: ${e && e.message ? e.message : String(e)}`);
  }
}

function getConfiguredPythonPath() {
  try {
    const cfg = vscode.workspace.getConfiguration('contextEngineUploader');
    const configured = (cfg.get('pythonPath') || 'python3').trim();
    if (pythonOverridePath && fs.existsSync(pythonOverridePath)) {
      return pythonOverridePath;
    }
    return configured || 'python3';
  } catch (_) {
    if (pythonOverridePath && fs.existsSync(pythonOverridePath)) {
      return pythonOverridePath;
    }
    return 'python3';
  }
}

function getConfiguredDecoderUrl() {
  try {
    const cfg = vscode.workspace.getConfiguration('contextEngineUploader');
    const configured = (cfg.get('decoderUrl') || '').trim();
    return configured || 'http://localhost:8081';
  } catch (_) {
    return 'http://localhost:8081';
  }
}

function resolveCtxScriptPath() {
  const candidates = [];
  candidates.push(path.join(extensionRoot, 'scripts', 'ctx.py'));
  candidates.push(path.join(extensionRoot, 'ctx.py'));
  const wsFolder = getWorkspaceFolderPath();
  if (wsFolder) {
    candidates.push(path.join(wsFolder, 'scripts', 'ctx.py'));
    candidates.push(path.join(wsFolder, 'ctx.py'));
  }
  candidates.push(path.resolve(extensionRoot, '..', '..', 'scripts', 'ctx.py'));

  for (const candidate of candidates) {
    if (candidate && fs.existsSync(candidate)) {
      return path.resolve(candidate);
    }
  }

  vscode.window.showErrorMessage('Context Engine Uploader: ctx.py not found (expected scripts/ctx.py).');
  return undefined;
}

async function enhanceSelectionWithUnicorn() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showWarningMessage('Open a file and select text to enhance with Prompt+.');
    return;
  }
  const selection = editor.selections && editor.selections.length ? editor.selections[0] : editor.selection;
  const text = selection && !selection.isEmpty ? editor.document.getText(selection) : '';
  if (!text || !text.trim()) {
    vscode.window.showWarningMessage('Select text to enhance with Prompt+.');
    return;
  }

  const ctxScript = resolveCtxScriptPath();
  if (!ctxScript) {
    return;
  }

  const pythonPath = getConfiguredPythonPath();
  const projectRoot = path.dirname(path.dirname(ctxScript));
  const env = { ...process.env };
  env.PYTHONUNBUFFERED = '1';
  const decoderUrl = getConfiguredDecoderUrl();
  if (decoderUrl) {
    env.DECODER_URL = decoderUrl;
  }
  try {
    const cfg = vscode.workspace.getConfiguration('contextEngineUploader');
    const useGpuDecoder = cfg.get('useGpuDecoder', false);
    if (useGpuDecoder) {
      env.USE_GPU_DECODER = '1';
    }
  } catch (_) {
    // ignore config read failures; fall back to defaults
  }
  const existingPyPath = env.PYTHONPATH || '';
  env.PYTHONPATH = existingPyPath ? `${projectRoot}${path.delimiter}${existingPyPath}` : projectRoot;

  log(`Running Prompt+ via ctx.py at ${ctxScript}`);

  return new Promise((resolve) => {
    const args = [ctxScript, '--unicorn', text];
    const child = spawn(pythonPath, args, { cwd: projectRoot, env });
    let stdout = '';
    let stderr = '';

    if (child.stdout) {
      child.stdout.on('data', data => {
        stdout += data.toString();
      });
    }
    if (child.stderr) {
      child.stderr.on('data', data => {
        const chunk = data.toString();
        stderr += chunk;
        if (outputChannel) {
          outputChannel.append(`[prompt+] ${chunk}`);
        }
      });
    }

    child.on('error', error => {
      log(`Prompt+ spawn failed: ${error instanceof Error ? error.message : String(error)}`);
      vscode.window.showErrorMessage('Prompt+ failed to start. Check Python path.');
      resolve();
    });

    child.on('close', code => {
      if (code !== 0) {
        log(`Prompt+ exited with code ${code}${stderr ? `: ${stderr.trim()}` : ''}`);
        vscode.window.showErrorMessage('Prompt+ failed. See output for details.');
        return resolve();
      }
      const enhanced = (stdout || '').trim();
      if (!enhanced) {
        vscode.window.showWarningMessage('Prompt+ returned no output.');
        return resolve();
      }
      editor.edit(editBuilder => editBuilder.replace(selection, enhanced)).then(ok => {
        if (ok) {
          vscode.window.showInformationMessage('Prompt+ applied (Unicorn Mode).');
        } else {
          vscode.window.showErrorMessage('Prompt+ could not update the selection.');
        }
        resolve();
      });
    });
  });
}

function ensureIndexedWatcher(targetPath) {
  try {
    disposeIndexedWatcher();
    watchedTargetPath = targetPath;
    let pattern;
    if (targetPath && fs.existsSync(targetPath)) {
      pattern = new vscode.RelativePattern(targetPath, '**/*');
    } else {
      const folder = getWorkspaceFolderPath();
      if (folder && fs.existsSync(folder)) {
        pattern = new vscode.RelativePattern(folder, '**/*');
      } else {
        pattern = '**/*';
      }
    }
    workspaceWatcher = vscode.workspace.createFileSystemWatcher(pattern, false, false, false);
    const flipToIdle = () => {
      if (statusMode === 'indexed') {
        setStatusBarState('idle');
      }
    };
    indexedWatchDisposables.push(workspaceWatcher);
    indexedWatchDisposables.push(workspaceWatcher.onDidCreate(flipToIdle));
    indexedWatchDisposables.push(workspaceWatcher.onDidChange(flipToIdle));
    indexedWatchDisposables.push(workspaceWatcher.onDidDelete(flipToIdle));
    indexedWatchDisposables.push(vscode.workspace.onDidChangeTextDocument(() => flipToIdle()));
    log('Indexed watcher armed; any file change will return status bar to "Index Codebase".');
  } catch (e) {
    log(`Failed to arm indexed watcher: ${e && e.message ? e.message : String(e)}`);
  }
}

function disposeIndexedWatcher() {
  try {
    for (const d of indexedWatchDisposables) {
      try { if (d && typeof d.dispose === 'function') d.dispose(); } catch (_) {}
    }
    indexedWatchDisposables = [];
    if (workspaceWatcher && typeof workspaceWatcher.dispose === 'function') {
      workspaceWatcher.dispose();
    }
    workspaceWatcher = undefined;
    watchedTargetPath = undefined;
  } catch (e) {
    // ignore
  }
}

async function stopProcesses() {
  await Promise.all([terminateProcess(forceProcess, 'force'), terminateProcess(watchProcess, 'watch')]);
  if (!forceProcess && !watchProcess && statusMode !== 'indexing') {
    setStatusBarState('idle');
  }
}
function terminateProcess(proc, label) {
  if (!proc) {
    return Promise.resolve();
  }
  return new Promise(resolve => {
    let finished = false;
    let termTimer;
    let killTimer;
    const cleanup = () => {
      if (termTimer) clearTimeout(termTimer);
      if (killTimer) clearTimeout(killTimer);
    };
    const finalize = (reason) => {
      if (finished) return;
      finished = true;
      cleanup();
      if (proc === forceProcess) {
        forceProcess = undefined;
      }
      if (proc === watchProcess) {
        watchProcess = undefined;
      }
      log(`${label} process stopped${reason ? ` (${reason})` : ''}.`);
      resolve();
    };

    // Resolve only after the child actually exits (or after forced kill path)
    const onExit = (code, signal) => {
      finalize(`exit code=${code} signal=${signal || ''}`.trim());
    };
    proc.once('exit', onExit);
    proc.once('close', onExit);

    try {
      proc.kill(); // default SIGTERM
    } catch (error) {
      finalize('kill() threw');
      return;
    }

    const waitSigtermMs = 4000;
    const waitSigkillMs = 2000;

    // If process doesn't exit after SIGTERM, escalate to SIGKILL and then force-resolve
    termTimer = setTimeout(() => {
      try {
        if (proc && !proc.killed) {
          proc.kill('SIGKILL');
          log(`${label} process did not exit after ${waitSigtermMs}ms; sent SIGKILL.`);
        }
      } catch (_) {
        // ignore
      }
      killTimer = setTimeout(() => {
        finalize(`forced after ${waitSigtermMs + waitSigkillMs}ms`);
      }, waitSigkillMs);
    }, waitSigtermMs);
  });
}
function log(message) {
  if (!outputChannel) {
    return;
  }
  const timestamp = new Date().toISOString();
  outputChannel.appendLine(`[${timestamp}] ${message}`);
}
function buildChildEnv(options) {
  const env = {
    ...process.env,
    WORKSPACE_PATH: options.targetPath,
    WATCH_ROOT: options.targetPath
  };
  try {
    const settings = vscode.workspace.getConfiguration('contextEngineUploader');
    const devRemoteMode = settings.get('devRemoteMode', false);
    if (devRemoteMode) {
      // Enable dev-remote upload mode for the standalone upload client.
      // This causes standalone_upload_client.py to ignore any 'dev-workspace'
      // directories when scanning for files to upload.
      env.REMOTE_UPLOAD_MODE = 'development';
      env.DEV_REMOTE_MODE = '1';
      log('Context Engine Uploader: devRemoteMode enabled (REMOTE_UPLOAD_MODE=development, DEV_REMOTE_MODE=1).');
    }
    const maxBundleSizeMb = settings.get('maxBundleSizeMb');
    if (typeof maxBundleSizeMb === 'number' && maxBundleSizeMb > 0) {
      env.REMOTE_UPLOAD_MAX_BUNDLE_MB = String(maxBundleSizeMb);
    }
    const requestTimeoutSeconds = settings.get('requestTimeoutSeconds');
    if (typeof requestTimeoutSeconds === 'number' && requestTimeoutSeconds > 0) {
      env.REMOTE_UPLOAD_TIMEOUT = String(requestTimeoutSeconds);
    }
  } catch (error) {
    log(`Failed to read devRemoteMode setting: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (options.hostRoot) {
    env.HOST_ROOT = options.hostRoot;
  }
  if (options.containerRoot) {
    env.CONTAINER_ROOT = options.containerRoot;
  }
  try {
    const libsPath = path.join(options.workingDirectory, 'python_libs');
    if (fs.existsSync(libsPath)) {
      const existing = process.env.PYTHONPATH || '';
      env.PYTHONPATH = existing ? `${libsPath}${path.delimiter}${existing}` : libsPath;
      log(`Detected bundled python_libs at ${libsPath}; setting PYTHONPATH for child process.`);
    }
  } catch (error) {
    log(`Failed to configure PYTHONPATH for bundled deps: ${error instanceof Error ? error.message : String(error)}`);
  }
  return env;
}
async function writeMcpConfig() {
  const settings = vscode.workspace.getConfiguration('contextEngineUploader');
  const claudeEnabled = settings.get('mcpClaudeEnabled', true);
  const windsurfEnabled = settings.get('mcpWindsurfEnabled', false);
  const claudeHookEnabled = settings.get('claudeHookEnabled', false);
  const isLinux = process.platform === 'linux';
  if (!claudeEnabled && !windsurfEnabled && !claudeHookEnabled) {
    vscode.window.showInformationMessage('Context Engine Uploader: MCP config writing is disabled in settings.');
    return;
  }
  const indexerUrl = (settings.get('mcpIndexerUrl') || 'http://localhost:8001/sse').trim();
  const memoryUrl = (settings.get('mcpMemoryUrl') || 'http://localhost:8000/sse').trim();
  let wroteAny = false;
  let hookWrote = false;
  if (claudeEnabled) {
    const root = getWorkspaceFolderPath();
    if (!root) {
      vscode.window.showErrorMessage('Context Engine Uploader: open a folder before writing .mcp.json.');
    } else {
      const result = await writeClaudeMcpServers(root, indexerUrl, memoryUrl);
      wroteAny = wroteAny || result;
    }
  }
  if (windsurfEnabled) {
    const customPath = (settings.get('windsurfMcpPath') || '').trim();
    const windsPath = customPath || getDefaultWindsurfMcpPath();
    const result = await writeWindsurfMcpServers(windsPath, indexerUrl, memoryUrl);
    wroteAny = wroteAny || result;
  }
  if (claudeHookEnabled) {
    const root = getWorkspaceFolderPath();
    if (!root) {
      vscode.window.showErrorMessage('Context Engine Uploader: open a folder before writing Claude hook config.');
    } else if (!isLinux) {
      vscode.window.showWarningMessage('Context Engine Uploader: Claude hook auto-config is only wired for Linux/dev-remote at this time.');
    } else {
      const commandPath = getClaudeHookCommand();
      if (!commandPath) {
        vscode.window.showErrorMessage('Context Engine Uploader: embedded Claude hook script not found in extension; .claude/settings.local.json was not updated.');
        log('Claude hook config skipped because embedded ctx-hook-simple.sh could not be resolved.');
      } else {
        const result = await writeClaudeHookConfig(root, commandPath);
        hookWrote = hookWrote || result;
      }
    }
  }
  if (!wroteAny && !hookWrote) {
    log('Context Engine Uploader: MCP config write skipped (no targets succeeded).');
  }

  // Optionally scaffold ctx_config.json and .env using the inferred collection
  if (settings.get('scaffoldCtxConfig', true)) {
    try {
      await writeCtxConfig();
    } catch (error) {
      log(`CTX config auto-scaffolding failed: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
}

async function writeCtxConfig() {
  const settings = vscode.workspace.getConfiguration('contextEngineUploader');
  const enabled = settings.get('scaffoldCtxConfig', true);
  if (!enabled) {
    vscode.window.showInformationMessage('Context Engine Uploader: ctx_config/.env scaffolding is disabled (contextEngineUploader.scaffoldCtxConfig=false).');
    log('CTX config scaffolding skipped because scaffoldCtxConfig is false.');
    return;
  }
  const options = resolveOptions();
  if (!options) {
    return;
  }
  const collectionName = inferCollectionFromUpload(options);
  if (!collectionName) {
    vscode.window.showErrorMessage('Context Engine Uploader: failed to infer collection name from upload client. Check the Output panel for details.');
    return;
  }
  await scaffoldCtxConfigFiles(options.targetPath, collectionName);
}

function inferCollectionFromUpload(options) {
  try {
    const args = ['-u', options.scriptPath, '--path', options.targetPath, '--endpoint', options.endpoint, '--show-mapping'];
    const result = spawnSync(options.pythonPath, args, {
      cwd: options.workingDirectory,
      env: buildChildEnv(options),
      encoding: 'utf8'
    });
    if (result.error) {
      log(`Failed to run standalone_upload_client for collection inference: ${result.error.message || String(result.error)}`);
      return undefined;
    }
    const stdout = result.stdout || '';
    const stderr = result.stderr || '';

    if (stdout) {
      log(`[ctx-config] upload client --show-mapping output:\n${stdout}`);
    }
    if (stderr) {
      log(`[ctx-config] upload client stderr:\n${stderr}`);
    }

    const combined = `${stdout}\n${stderr}`;
    if (combined.trim()) {
      const lines = combined.split(/\r?\n/);
      for (const line of lines) {
        const m = line.match(/collection_name:\s*(.+)$/);
        if (m && m[1]) {
          const name = m[1].trim();
          if (name) {
            return name;
          }
        }
      }
    }
  } catch (error) {
    log(`Error inferring collection from upload client: ${error instanceof Error ? error.message : String(error)}`);
  }
  return undefined;
}

async function scaffoldCtxConfigFiles(workspaceDir, collectionName) {
  try {
    const placeholders = new Set(['', 'default-collection', 'my-collection', 'codebase']);

    let uploaderSettings;
    try {
      uploaderSettings = vscode.workspace.getConfiguration('contextEngineUploader');
    } catch (error) {
      log(`Failed to read uploader settings: ${error instanceof Error ? error.message : String(error)}`);
      uploaderSettings = undefined;
    }

    // Decoder/runtime settings from configuration
    let decoderRuntime = 'glm';
    let useGpuDecoderSetting = false;
    let glmApiKey = '';
    let glmApiBase = 'https://api.z.ai/api/coding/paas/v4/';
    let glmModel = 'glm-4.6';
    if (uploaderSettings) {
      try {
        const runtimeSetting = String(uploaderSettings.get('decoderRuntime') ?? 'glm').trim().toLowerCase();
        if (runtimeSetting === 'llamacpp') {
          decoderRuntime = 'llamacpp';
        }
        useGpuDecoderSetting = !!uploaderSettings.get('useGpuDecoder', false);
        const cfgKey = (uploaderSettings.get('glmApiKey') || '').trim();
        const cfgBase = (uploaderSettings.get('glmApiBase') || '').trim();
        const cfgModel = (uploaderSettings.get('glmModel') || '').trim();
        if (cfgKey) {
          glmApiKey = cfgKey;
        }
        if (cfgBase) {
          glmApiBase = cfgBase;
        }
        if (cfgModel) {
          glmModel = cfgModel;
        }
      } catch (error) {
        log(`Failed to read decoder/GLM settings from configuration: ${error instanceof Error ? error.message : String(error)}`);
      }
    }

    // ctx_config.json
    const ctxConfigPath = path.join(workspaceDir, 'ctx_config.json');
    let ctxConfig = {};
    if (fs.existsSync(ctxConfigPath)) {
      try {
        const raw = fs.readFileSync(ctxConfigPath, 'utf8');
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object') {
          ctxConfig = parsed;
        }
      } catch (error) {
        log(`Failed to parse existing ctx_config.json at ${ctxConfigPath}; overwriting with minimal config. Error: ${error instanceof Error ? error.message : String(error)}`);
        ctxConfig = {};
      }
    }
    const currentDefault = typeof ctxConfig.default_collection === 'string' ? ctxConfig.default_collection.trim() : '';
    let ctxChanged = false;
    let notifiedDefault = false;
    if (!currentDefault || placeholders.has(currentDefault)) {
      ctxConfig.default_collection = collectionName;
      ctxChanged = true;
      notifiedDefault = true;
    }
    if (ctxConfig.default_mode === undefined) {
      ctxConfig.default_mode = 'default';
      ctxChanged = true;
    }
    if (ctxConfig.require_context === undefined) {
      ctxConfig.require_context = true;
      ctxChanged = true;
    }
    if (ctxConfig.surface_qdrant_collection_hint === undefined) {
      let surfaceHintSetting = true;
      if (uploaderSettings) {
        try {
          surfaceHintSetting = !!uploaderSettings.get('surfaceQdrantCollectionHint', true);
        } catch (error) {
          log(`Failed to read surfaceQdrantCollectionHint from configuration: ${error instanceof Error ? error.message : String(error)}`);
        }
      }
      ctxConfig.surface_qdrant_collection_hint = surfaceHintSetting;
      ctxChanged = true;
    }
    if (ctxConfig.refrag_runtime !== decoderRuntime) {
      ctxConfig.refrag_runtime = decoderRuntime;
      ctxChanged = true;
    }
    if (decoderRuntime === 'glm') {
      if (ctxConfig.glm_api_base === undefined) {
        ctxConfig.glm_api_base = glmApiBase;
        ctxChanged = true;
      }
      if (ctxConfig.glm_model === undefined) {
        ctxConfig.glm_model = glmModel;
        ctxChanged = true;
      }
      const existingGlmKey = typeof ctxConfig.glm_api_key === 'string' ? ctxConfig.glm_api_key.trim() : '';
      if (glmApiKey) {
        if (!existingGlmKey) {
          ctxConfig.glm_api_key = glmApiKey;
          ctxChanged = true;
        }
      } else if (ctxConfig.glm_api_key === undefined) {
        ctxConfig.glm_api_key = '';
        ctxChanged = true;
      }
    }
    if (ctxChanged) {
      fs.writeFileSync(ctxConfigPath, JSON.stringify(ctxConfig, null, 2) + '\n', 'utf8');
      if (notifiedDefault) {
        vscode.window.showInformationMessage(`Context Engine Uploader: ctx_config.json updated with default_collection=${collectionName}.`);
      } else {
        vscode.window.showInformationMessage('Context Engine Uploader: ctx_config.json refreshed with required defaults.');
      }
      log(`Wrote ctx_config.json at ${ctxConfigPath}`);
    } else {
      log(`ctx_config.json at ${ctxConfigPath} already satisfied required values; not modified.`);
    }

    // .env
    const envPath = path.join(workspaceDir, '.env');
    let envContent = '';

    // Seed from bundled env.example (extension root) when workspace .env is missing
    const baseDir = extensionRoot || __dirname;
    const envExamplePath = path.join(baseDir, 'env.example');
    if (fs.existsSync(envPath)) {
      try {
        envContent = fs.readFileSync(envPath, 'utf8');
      } catch (error) {
        log(`Failed to read existing .env at ${envPath}; skipping .env update. Error: ${error instanceof Error ? error.message : String(error)}`);
        return;
      }
    } else if (fs.existsSync(envExamplePath)) {
      try {
        envContent = fs.readFileSync(envExamplePath, 'utf8');
        log(`Seeding new .env for ${workspaceDir} from bundled env.example.`);
      } catch (error) {
        log(`Failed to read bundled env.example at ${envExamplePath}; starting with minimal .env. Error: ${error instanceof Error ? error.message : String(error)}`);
        envContent = '';
      }
    }
    let envLines = envContent ? envContent.split(/\r?\n/) : [];
    let envChanged = false;
    let collectionUpdated = false;

    let idx = -1;
    for (let i = 0; i < envLines.length; i++) {
      if (envLines[i].trim().startsWith('COLLECTION_NAME=')) {
        idx = i;
        break;
      }
    }
    let currentEnvVal = '';
    if (idx >= 0) {
      const m = envLines[idx].match(/^COLLECTION_NAME=(.*)$/);
      if (m) {
        currentEnvVal = (m[1] || '').trim();
      }
    }
    if (idx === -1 || placeholders.has(currentEnvVal)) {
      const newLine = `COLLECTION_NAME=${collectionName}`;
      if (idx === -1) {
        if (envLines.length && envLines[envLines.length - 1].trim() !== '') {
          envLines.push('');
        }
        envLines.push(newLine);
      } else {
        envLines[idx] = newLine;
      }
      envChanged = true;
      collectionUpdated = true;
      vscode.window.showInformationMessage(`Context Engine Uploader: .env updated with COLLECTION_NAME=${collectionName}.`);
      log(`Updated .env at ${envPath}`);
    } else {
      log(`.env at ${envPath} already has non-placeholder COLLECTION_NAME; not modified.`);
    }

    function getEnvEntry(key) {
      for (let i = 0; i < envLines.length; i++) {
        const line = envLines[i];
        if (!line || line.trim().startsWith('#')) {
          continue;
        }
        const eqIndex = line.indexOf('=');
        if (eqIndex === -1) {
          continue;
        }
        const candidate = line.slice(0, eqIndex).trim();
        if (candidate === key) {
          return { index: i, value: line.slice(eqIndex + 1) };
        }
      }
      return { index: -1, value: undefined };
    }

    function upsertEnv(key, desiredValue, options = {}) {
      const {
        overwrite = false,
        treatEmptyAsUnset = false,
        placeholderValues = [],
        skipIfDesiredEmpty = false
      } = options;
      const desired = desiredValue ?? '';
      const desiredStr = String(desired);
      if (!desiredStr && skipIfDesiredEmpty) {
        return false;
      }
      const { index, value } = getEnvEntry(key);
      const current = typeof value === 'string' ? value.trim() : '';
      const normalizedDesired = desiredStr.trim();
      const placeholderSet = new Set((placeholderValues || []).map(val => (val || '').trim().toLowerCase()));
      let shouldUpdate = false;

      if (index === -1) {
        shouldUpdate = true;
      } else if (overwrite) {
        if (current !== normalizedDesired) {
          shouldUpdate = true;
        }
      } else if (treatEmptyAsUnset && !current) {
        shouldUpdate = true;
      } else if (placeholderSet.size && placeholderSet.has(current.toLowerCase())) {
        shouldUpdate = true;
      }

      if (!shouldUpdate) {
        return false;
      }

      const newLine = `${key}=${desiredStr}`;
      if (index === -1) {
        if (envLines.length && envLines[envLines.length - 1].trim() !== '') {
          envLines.push('');
        }
        envLines.push(newLine);
      } else {
        envLines[index] = newLine;
      }
      envChanged = true;
      return true;
    }

    // Force CTX-critical defaults regardless of template values
    upsertEnv('MULTI_REPO_MODE', '1', { overwrite: true });
    upsertEnv('REFRAG_MODE', '1', { overwrite: true });
    upsertEnv('REFRAG_DECODER', '1', { overwrite: true });
    upsertEnv('REFRAG_RUNTIME', decoderRuntime, { overwrite: true, placeholderValues: ['llamacpp', 'glm'] });
    upsertEnv('USE_GPU_DECODER', useGpuDecoderSetting ? '1' : '0', { overwrite: true });

    // Ensure decoder/GLM env vars exist with sane defaults
    upsertEnv('REFRAG_ENCODER_MODEL', 'BAAI/bge-base-en-v1.5', { treatEmptyAsUnset: true });
    upsertEnv('REFRAG_PHI_PATH', '/work/models/refrag_phi_768_to_dmodel.bin', { treatEmptyAsUnset: true });
    upsertEnv('REFRAG_SENSE', 'heuristic', { treatEmptyAsUnset: true });

    if (decoderRuntime === 'glm') {
      const glmKeyPlaceholders = ['YOUR_GLM_API_KEY', '"YOUR_GLM_API_KEY"', "''", '""'];
      if (glmApiKey) {
        upsertEnv('GLM_API_KEY', glmApiKey, {
          treatEmptyAsUnset: true,
          placeholderValues: glmKeyPlaceholders
        });
      } else {
        upsertEnv('GLM_API_KEY', '', {});
      }
      upsertEnv('GLM_API_BASE', glmApiBase, { treatEmptyAsUnset: true });
      upsertEnv('GLM_MODEL', glmModel, { treatEmptyAsUnset: true });
    }

    // Ensure MCP_INDEXER_URL is present based on extension setting (for ctx.py)
    if (uploaderSettings) {
      try {
        const ctxIndexerUrl = (uploaderSettings.get('ctxIndexerUrl') || 'http://localhost:8003/mcp').trim();
        if (ctxIndexerUrl) {
          upsertEnv('MCP_INDEXER_URL', ctxIndexerUrl, { treatEmptyAsUnset: true });
        }
      } catch (error) {
        log(`Failed to read ctxIndexerUrl setting for MCP_INDEXER_URL: ${error instanceof Error ? error.message : String(error)}`);
      }
    }

    if (envChanged) {
      fs.writeFileSync(envPath, envLines.join('\n') + '\n', 'utf8');
      log(`Ensured decoder/GLM/MCP settings in .env at ${envPath}`);
    } else {
      log(`.env at ${envPath} already satisfied CTX defaults; not modified.`);
    }
  } catch (error) {
    log(`Error scaffolding ctx_config/.env: ${error instanceof Error ? error.message : String(error)}`);
  }
}
function deactivate() {
  disposeIndexedWatcher();
  return stopProcesses();
}
module.exports = {
  activate,
  deactivate
};

function getClaudeHookCommand() {
  const isLinux = process.platform === 'linux';
  if (!isLinux) {
    return '';
  }
  if (!extensionRoot) {
    log('Claude hook command resolution failed: extensionRoot is undefined.');
    return '';
  }
  try {
    const embeddedPath = path.join(extensionRoot, 'ctx-hook-simple.sh');
    if (fs.existsSync(embeddedPath)) {
      log(`Using embedded Claude hook at ${embeddedPath}`);
      return embeddedPath;
    }
    log(`Claude hook command resolution failed: ctx-hook-simple.sh not found at ${embeddedPath}`);
  } catch (error) {
    log(`Failed to resolve embedded Claude hook path: ${error instanceof Error ? error.message : String(error)}`);
  }
  return '';
}

function getDefaultWindsurfMcpPath() {
  return path.join(os.homedir(), '.codeium', 'windsurf', 'mcp_config.json');
}

async function writeClaudeMcpServers(root, indexerUrl, memoryUrl) {
  const configPath = path.join(root, '.mcp.json');
  let config = { mcpServers: {} };
  if (fs.existsSync(configPath)) {
    try {
      const raw = fs.readFileSync(configPath, 'utf8');
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {
        config = parsed;
      }
    } catch (error) {
      vscode.window.showErrorMessage('Context Engine Uploader: existing .mcp.json is invalid JSON; not modified.');
      log(`Failed to parse .mcp.json: ${error instanceof Error ? error.message : String(error)}`);
      return false;
    }
  }
  if (!config.mcpServers || typeof config.mcpServers !== 'object') {
    config.mcpServers = {};
  }
  log(`Preparing to write .mcp.json at ${configPath} with indexerUrl=${indexerUrl || '""'} memoryUrl=${memoryUrl || '""'}`);
  const isWindows = process.platform === 'win32';
  const makeServer = url => {
    if (isWindows) {
      return {
        command: 'cmd',
        args: ['/c', 'npx', 'mcp-remote', url, '--transport', 'sse-only'],
        env: {}
      };
    }
    return {
      command: 'npx',
      args: ['mcp-remote', url, '--transport', 'sse-only'],
      env: {}
    };
  };
  const servers = config.mcpServers;
  if (indexerUrl) {
    servers['qdrant-indexer'] = makeServer(indexerUrl);
  }
  if (memoryUrl) {
    servers.memory = makeServer(memoryUrl);
  }
  try {
    const json = JSON.stringify(config, null, 2) + '\n';
    fs.writeFileSync(configPath, json, 'utf8');
    vscode.window.showInformationMessage('Context Engine Uploader: .mcp.json updated for Context Engine MCP servers.');
    log(`Wrote .mcp.json at ${configPath}`);
    return true;
  } catch (error) {
    vscode.window.showErrorMessage('Context Engine Uploader: failed to write .mcp.json.');
    log(`Failed to write .mcp.json: ${error instanceof Error ? error.message : String(error)}`);
    return false;
  }
}

async function writeWindsurfMcpServers(configPath, indexerUrl, memoryUrl) {
  try {
    fs.mkdirSync(path.dirname(configPath), { recursive: true });
  } catch (error) {
    log(`Failed to ensure Windsurf MCP directory: ${error instanceof Error ? error.message : String(error)}`);
    vscode.window.showErrorMessage('Context Engine Uploader: failed to prepare Windsurf MCP directory.');
    return false;
  }
  let config = { mcpServers: {} };
  if (fs.existsSync(configPath)) {
    try {
      const raw = fs.readFileSync(configPath, 'utf8');
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {
        config = parsed;
      }
    } catch (error) {
      vscode.window.showErrorMessage('Context Engine Uploader: existing Windsurf mcp_config.json is invalid JSON; not modified.');
      log(`Failed to parse Windsurf mcp_config.json: ${error instanceof Error ? error.message : String(error)}`);
      return false;
    }
  }
  if (!config.mcpServers || typeof config.mcpServers !== 'object') {
    config.mcpServers = {};
  }
  log(`Preparing to write Windsurf mcp_config.json at ${configPath} with indexerUrl=${indexerUrl || '""'} memoryUrl=${memoryUrl || '""'}`);
  const makeServer = url => ({
    command: 'npx',
    args: ['mcp-remote', url, '--transport', 'sse-only'],
    env: {}
  });
  const servers = config.mcpServers;
  if (indexerUrl) {
    servers['qdrant-indexer'] = makeServer(indexerUrl);
  }
  if (memoryUrl) {
    servers.memory = makeServer(memoryUrl);
  }
  try {
    const json = JSON.stringify(config, null, 2) + '\n';
    fs.writeFileSync(configPath, json, 'utf8');
    vscode.window.showInformationMessage(`Context Engine Uploader: Windsurf MCP config updated at ${configPath}.`);
    log(`Wrote Windsurf mcp_config.json at ${configPath}`);
    return true;
  } catch (error) {
    vscode.window.showErrorMessage('Context Engine Uploader: failed to write Windsurf mcp_config.json.');
    log(`Failed to write Windsurf mcp_config.json: ${error instanceof Error ? error.message : String(error)}`);
    return false;
  }
}

async function writeClaudeHookConfig(root, commandPath) {
  try {
    const claudeDir = path.join(root, '.claude');
    fs.mkdirSync(claudeDir, { recursive: true });
    const settingsPath = path.join(claudeDir, 'settings.local.json');
    let config = {};
    if (fs.existsSync(settingsPath)) {
      try {
        const raw = fs.readFileSync(settingsPath, 'utf8');
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object') {
          config = parsed;
        }
      } catch (error) {
        vscode.window.showErrorMessage('Context Engine Uploader: existing .claude/settings.local.json is invalid JSON; not modified.');
        log(`Failed to parse .claude/settings.local.json: ${error instanceof Error ? error.message : String(error)}`);
        return false;
      }
    }
    if (!config.permissions || typeof config.permissions !== 'object') {
      config.permissions = { allow: [], deny: [], ask: [] };
    } else {
      config.permissions.allow = config.permissions.allow || [];
      config.permissions.deny = config.permissions.deny || [];
      config.permissions.ask = config.permissions.ask || [];
    }
    if (!config.enabledMcpjsonServers) {
      config.enabledMcpjsonServers = [];
    }
    if (!config.hooks || typeof config.hooks !== 'object') {
      config.hooks = {};
    }
    // Derive CTX workspace directory for the hook from extension settings.
    // Collection hint behavior is now driven by ctx_config.json, not hook env.
    let hookEnv;
    try {
      const uploaderConfig = vscode.workspace.getConfiguration('contextEngineUploader');
      const targetPath = (uploaderConfig.get('targetPath') || '').trim();
      if (targetPath) {
        const resolvedTarget = path.resolve(targetPath);
        hookEnv = { CTX_WORKSPACE_DIR: resolvedTarget };
      }
    } catch (error) {
      // Best-effort only; if anything fails, fall back to no extra env
      hookEnv = undefined;
    }

    const hook = {
      type: 'command',
      command: commandPath
    };
    if (hookEnv) {
      hook.env = hookEnv;
    }

    // Append or update our hook under UserPromptSubmit without clobbering existing hooks
    let userPromptHooks = config.hooks['UserPromptSubmit'];
    if (!Array.isArray(userPromptHooks)) {
      userPromptHooks = [];
    }

    const normalizeCommand = value => {
      if (!value) return '';
      const resolved = path.resolve(value);
      return resolved.replace(/context-engine\.context-engine-uploader-[0-9.]+/, 'context-engine.context-engine-uploader');
    };

    const normalizedNewCommand = normalizeCommand(commandPath);
    let updated = false;

    for (const entry of userPromptHooks) {
      if (!entry || !Array.isArray(entry.hooks)) {
        continue;
      }
      for (const existing of entry.hooks) {
        if (!existing || existing.type !== 'command') {
          continue;
        }
        const normalizedExisting = normalizeCommand(existing.command);
        if (normalizedExisting === normalizedNewCommand) {
          existing.command = commandPath;
          if (!existing.env) {
            existing.env = {};
          }
          if (hookEnv) {
            existing.env = { ...existing.env, ...hookEnv };
          }
          updated = true;
        }
      }
    }

    if (!updated) {
      userPromptHooks.push({ hooks: [hook] });
    }

    // Deduplicate any accidental double entries for the same command
    const seenCommands = new Set();
    for (const entry of userPromptHooks) {
      if (!entry || !Array.isArray(entry.hooks)) {
        continue;
      }
      entry.hooks = entry.hooks.filter(existing => {
        if (!existing || existing.type !== 'command') {
          return true;
        }
        const normalized = normalizeCommand(existing.command);
        if (seenCommands.has(normalized)) {
          return false;
        }
        seenCommands.add(normalized);
        return true;
      });
    }

    config.hooks['UserPromptSubmit'] = userPromptHooks.filter(entry => Array.isArray(entry.hooks) && entry.hooks.length);
    fs.writeFileSync(settingsPath, JSON.stringify(config, null, 2) + '\n', 'utf8');
    vscode.window.showInformationMessage('Context Engine Uploader: .claude/settings.local.json updated with Claude hook.');
    log(`Wrote Claude hook config at ${settingsPath}`);
    return true;
  } catch (error) {
    vscode.window.showErrorMessage('Context Engine Uploader: failed to write .claude/settings.local.json.');
    log(`Failed to write .claude/settings.local.json: ${error instanceof Error ? error.message : String(error)}`);
    return false;
  }
}
