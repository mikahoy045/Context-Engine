function createPromptPlusManager(deps) {
  const vscode = deps.vscode;
  const spawn = deps.spawn;
  const path = deps.path;
  const fs = deps.fs;
  const log = deps.log;

  const extensionRoot = deps.extensionRoot;
  const getEffectiveConfig = deps.getEffectiveConfig;
  const getTargetPath = deps.getTargetPath;
  const getWorkspaceFolderPath = deps.getWorkspaceFolderPath;
  const detectDefaultTargetPath = deps.detectDefaultTargetPath;
  const resolveBridgeHttpUrl = deps.resolveBridgeHttpUrl;
  const getPythonOverridePath = deps.getPythonOverridePath;
  const appendOutput = deps.appendOutput;

  function dispose() {
    // No timers/processes owned.
  }

  function getConfiguredPythonPath() {
    try {
      const cfg = getEffectiveConfig();
      const configured = (cfg.get('pythonPath') || 'python3').trim();
      const override = typeof getPythonOverridePath === 'function' ? getPythonOverridePath() : undefined;
      if (override && fs.existsSync(override)) {
        return override;
      }
      return configured || 'python3';
    } catch (_) {
      const override = typeof getPythonOverridePath === 'function' ? getPythonOverridePath() : undefined;
      if (override && fs.existsSync(override)) {
        return override;
      }
      return 'python3';
    }
  }

  function getConfiguredDecoderUrl() {
    try {
      const cfg = getEffectiveConfig();
      const configured = (cfg.get('decoderUrl') || '').trim();
      return configured || 'http://localhost:8081';
    } catch (_) {
      return 'http://localhost:8081';
    }
  }

  function resolveCtxScriptPath() {
    const candidates = [];
    
    candidates.push(path.join(extensionRoot, 'ctx.py'));
    candidates.push(path.join(extensionRoot, 'scripts', 'ctx.py'));
    
    const wsFolder = getWorkspaceFolderPath();
    if (wsFolder) {
      candidates.push(path.join(wsFolder, 'ctx.py'));
      candidates.push(path.join(wsFolder, 'scripts', 'ctx.py'));
    }
    
    candidates.push(path.resolve(extensionRoot, '..', '..', 'scripts', 'ctx.py'));
    
    try {
      const repoRoot = path.resolve(extensionRoot, '..', '..');
      const repoScripts = path.join(repoRoot, 'scripts', 'ctx.py');
      if (!candidates.includes(repoScripts)) {
        candidates.push(repoScripts);
      }
    } catch (_) {}

    for (const candidate of candidates) {
      if (candidate && fs.existsSync(candidate)) {
        const scriptsDir = path.dirname(candidate);
        const mcpRouterPath = path.join(scriptsDir, 'mcp_router.py');
        if (fs.existsSync(mcpRouterPath)) {
          log(`Found ctx.py with dependencies at ${candidate}`);
          return path.resolve(candidate);
        }
        log(`Found ctx.py at ${candidate} but missing mcp_router.py dependency`);
      }
    }

    vscode.window.showErrorMessage('Context Engine Uploader: ctx.py not found or missing dependencies (mcp_router.py).');
    return undefined;
  }

  function getActiveSelectionText() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      return { editor: undefined, selection: undefined, text: '' };
    }
    const selection = editor.selections && editor.selections.length ? editor.selections[0] : editor.selection;
    const text = selection && !selection.isEmpty ? editor.document.getText(selection) : '';
    return { editor, selection, text };
  }

  async function resolveInputTextFromUser(selectionText) {
    const selectionTrimmed = (selectionText || '').trim();
    if (selectionTrimmed) {
      const picked = await vscode.window.showQuickPick(
        [
          { label: 'Use selection', id: 'selection' },
          { label: 'Enter message', id: 'message' },
          { label: 'Enter message + selection', id: 'message+selection' },
        ],
        { placeHolder: 'Prompt+ input source' }
      );
      if (!picked) {
        return undefined;
      }
      if (picked.id === 'selection') {
        return selectionText;
      }
      const message = await vscode.window.showInputBox({
        prompt: 'Prompt+ message',
        placeHolder: 'Type an instruction or question to enhance with Context Engine',
        ignoreFocusOut: true,
      });
      const msgTrimmed = (message || '').trim();
      if (!msgTrimmed) {
        return undefined;
      }
      if (picked.id === 'message') {
        return msgTrimmed;
      }
      return `${msgTrimmed}\n\n${selectionText}`;
    }

    const message = await vscode.window.showInputBox({
      prompt: 'Prompt+ message',
      placeHolder: 'Type an instruction or question to enhance with Context Engine',
      ignoreFocusOut: true,
    });
    const msgTrimmed = (message || '').trim();
    if (!msgTrimmed) {
      return undefined;
    }
    return msgTrimmed;
  }

  function normalizePromptMode(raw) {
    const v = (raw || '').trim().toLowerCase();
    if (v === 'unicorn') {
      return 'unicorn';
    }
    return 'default';
  }

  function readDefaultModeFromCtxConfig(ctxWorkspaceDir) {
    try {
      if (!ctxWorkspaceDir || typeof ctxWorkspaceDir !== 'string') {
        return undefined;
      }
      const cfgPath = path.join(ctxWorkspaceDir, 'ctx_config.json');
      if (!fs.existsSync(cfgPath)) {
        return undefined;
      }
      const raw = fs.readFileSync(cfgPath, 'utf8');
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') {
        return undefined;
      }
      if (typeof parsed.default_mode === 'string') {
        return normalizePromptMode(parsed.default_mode);
      }
      return undefined;
    } catch (_) {
      return undefined;
    }
  }

  function runPrompt(text) {
    const input = (text || '').trim();
    if (!input) {
      return Promise.resolve(undefined);
    }
    const ctxScript = resolveCtxScriptPath();
    if (!ctxScript) {
      return Promise.resolve(undefined);
    }

    const pythonPath = getConfiguredPythonPath();
    const ctxDir = path.dirname(ctxScript);
    const ctxBasename = path.basename(path.dirname(ctxScript));
    const isInScriptsDir = ctxBasename === 'scripts';
    const projectRoot = isInScriptsDir ? path.dirname(ctxDir) : ctxDir;
    
    const env = { ...process.env };
    env.PYTHONUNBUFFERED = '1';
    const decoderUrl = getConfiguredDecoderUrl();
    if (decoderUrl) {
      env.DECODER_URL = decoderUrl;
    }

    try {
      const cfg = getEffectiveConfig();
      const transportModeRaw = cfg.get('mcpTransportMode') || 'sse-remote';
      const serverModeRaw = cfg.get('mcpServerMode') || 'bridge';
      const transportMode = (typeof transportModeRaw === 'string' ? transportModeRaw.trim() : 'sse-remote') || 'sse-remote';
      const serverMode = (typeof serverModeRaw === 'string' ? serverModeRaw.trim() : 'bridge') || 'bridge';
      let idxUrlRaw = (cfg.get('ctxIndexerUrl') || cfg.get('mcpIndexerUrl') || '').trim();
      if (serverMode === 'bridge' && transportMode === 'http') {
        const bridgeUrl = resolveBridgeHttpUrl();
        if (bridgeUrl) {
          idxUrlRaw = bridgeUrl;
        }
      }
      if (idxUrlRaw) {
        env.MCP_INDEXER_URL = idxUrlRaw;
      }
    } catch (_) {
    }

    let ctxWorkspaceDir;
    try {
      const cfg = getEffectiveConfig();
      const useGpuDecoder = cfg.get('useGpuDecoder', false);
      if (useGpuDecoder) {
        env.USE_GPU_DECODER = '1';
      }
      try {
        ctxWorkspaceDir = getTargetPath(cfg);
      } catch (error) {
        ctxWorkspaceDir = undefined;
      }
      if (!ctxWorkspaceDir) {
        const wsFolder = getWorkspaceFolderPath();
        if (wsFolder) {
          ctxWorkspaceDir = detectDefaultTargetPath(wsFolder);
        }
      }
      if (ctxWorkspaceDir && typeof ctxWorkspaceDir === 'string' && fs.existsSync(ctxWorkspaceDir)) {
        env.CTX_WORKSPACE_DIR = ctxWorkspaceDir;
      }
    } catch (_) {
      ctxWorkspaceDir = undefined;
    }

    const existingPyPath = env.PYTHONPATH || '';
    const pathsToAdd = [ctxDir];
    if (isInScriptsDir && ctxDir !== projectRoot) {
      pathsToAdd.push(projectRoot);
    }
    const newPythonPath = pathsToAdd.join(path.delimiter);
    env.PYTHONPATH = existingPyPath ? `${newPythonPath}${path.delimiter}${existingPyPath}` : newPythonPath;
    
    log(`PYTHONPATH set to: ${env.PYTHONPATH}`);

    const configuredMode = readDefaultModeFromCtxConfig(ctxWorkspaceDir);
    const modeUsed = configuredMode || 'default';

    log(`Running Prompt+ via ctx.py at ${ctxScript} (mode=${modeUsed})`);

    return new Promise((resolve) => {
      const args = [ctxScript];
      if (modeUsed === 'unicorn') {
        args.push('--unicorn');
      }
      args.push(input);
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
          try {
            if (typeof appendOutput === 'function') {
              appendOutput(`[prompt+] ${chunk}`);
            }
          } catch (_) {
          }
        });
      }

      child.on('error', error => {
        log(`Prompt+ spawn failed: ${error instanceof Error ? error.message : String(error)}`);
        vscode.window.showErrorMessage('Prompt+ failed to start. Check Python path.');
        resolve(undefined);
      });

      child.on('close', code => {
        if (code !== 0) {
          log(`Prompt+ exited with code ${code}${stderr ? `: ${stderr.trim()}` : ''}`);
          vscode.window.showErrorMessage('Prompt+ failed. See output for details.');
          return resolve(undefined);
        }
        const enhanced = (stdout || '').trim();
        if (!enhanced) {
          vscode.window.showWarningMessage('Prompt+ returned no output.');
          return resolve(undefined);
        }
        resolve({ enhanced, modeUsed });
      });
    });
  }

  async function enhanceSelectionWithUnicorn() {
    const { editor, selection, text } = getActiveSelectionText();
    if (!editor) {
      vscode.window.showWarningMessage('Open a file and select text to enhance with Prompt+.');
      return;
    }
    if (!selection || selection.isEmpty || !text || !text.trim()) {
      vscode.window.showWarningMessage('Select text to enhance with Prompt+.');
      return;
    }
    const result = await runPrompt(text);
    if (!result || !result.enhanced) {
      return;
    }
    const modeLabel = result.modeUsed === 'unicorn' ? 'Unicorn Mode' : 'Default Mode';
    await editor.edit(editBuilder => editBuilder.replace(selection, result.enhanced)).then(ok => {
      if (ok) {
        vscode.window.showInformationMessage(`Prompt+ applied (${modeLabel}).`);
      } else {
        vscode.window.showErrorMessage('Prompt+ could not update the selection.');
      }
    });
  }

  async function enhancePromptWithUnicornCopy() {
    const { text } = getActiveSelectionText();
    const input = await resolveInputTextFromUser(text);
    if (!input) {
      return;
    }
    const result = await runPrompt(input);
    if (!result || !result.enhanced) {
      return;
    }
    const modeLabel = result.modeUsed === 'unicorn' ? 'Unicorn Mode' : 'Default Mode';
    await vscode.env.clipboard.writeText(result.enhanced);
    vscode.window.showInformationMessage(`Prompt+ copied to clipboard (${modeLabel}).`);
  }

  async function enhancePromptWithUnicornOpen() {
    const { text } = getActiveSelectionText();
    const input = await resolveInputTextFromUser(text);
    if (!input) {
      return;
    }
    const result = await runPrompt(input);
    if (!result || !result.enhanced) {
      return;
    }
    const doc = await vscode.workspace.openTextDocument({ content: result.enhanced, language: 'markdown' });
    await vscode.window.showTextDocument(doc, { preview: true });
  }

  return {
    enhanceSelectionWithUnicorn,
    enhancePromptWithUnicornCopy,
    enhancePromptWithUnicornOpen,
    dispose,
  };
}

module.exports = {
  createPromptPlusManager,
};
