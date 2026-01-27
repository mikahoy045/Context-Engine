const fs = require('fs');
const path = require('path');

let _vscode;
let _context;
let _log;
let _onProfileChanged;

const PROFILES_DB_FILENAME = 'profiles.json';
const PROFILES_DB_SETTING_KEY = 'profilesDb';
const PROFILES_DB_KEY = 'contextEngineUploader.profilesDb';
// TODO: Decide whether ACTIVE_PROFILE_KEY should be persisted in settings (shared across local/remote) or remain workspace-specific after verifying profilesDb carryover.
const ACTIVE_PROFILE_KEY = 'contextEngineUploader.activeProfileId';

function init({ vscode, context, log, onProfileChanged }) {
  _vscode = vscode;
  _context = context;
  _log = typeof log === 'function' ? log : () => { };
  _onProfileChanged = typeof onProfileChanged === 'function' ? onProfileChanged : () => { };
  try {
    if (_context && _context.globalState && typeof _context.globalState.setKeysForSync === 'function') {
      _context.globalState.setKeysForSync([PROFILES_DB_KEY, ACTIVE_PROFILE_KEY]);
    }
  } catch (_) {
  }
  ensureGlobalStorageDir();
}

function loadProfilesDbFromGlobalState() {
  try {
    if (!_context || !_context.globalState) {
      return undefined;
    }
    const raw = _context.globalState.get(PROFILES_DB_KEY);
    if (raw && typeof raw === 'object' && Array.isArray(raw.profiles)) {
      return { version: 1, ...raw };
    }
  } catch (_) {
  }
  return undefined;
}

function loadProfilesDbFromSettings() {
  try {
    if (!_vscode || !_vscode.workspace || typeof _vscode.workspace.getConfiguration !== 'function') {
      return undefined;
    }
    const cfg = _vscode.workspace.getConfiguration('contextEngineUploader');
    let raw;
    try {
      if (cfg && typeof cfg.inspect === 'function') {
        const inspected = cfg.inspect(PROFILES_DB_SETTING_KEY);
        if (inspected) {
          if (inspected.workspaceFolderValue !== undefined) {
            raw = inspected.workspaceFolderValue;
          } else if (inspected.workspaceValue !== undefined) {
            raw = inspected.workspaceValue;
          } else if (inspected.globalValue !== undefined) {
            raw = inspected.globalValue;
          }
        }
      }
    } catch (_) {
    }
    if (raw && typeof raw === 'object' && Array.isArray(raw.profiles)) {
      return { version: 1, ...raw };
    }
  } catch (_) {
  }
  return undefined;
}

async function saveProfilesDbToGlobalState(db) {
  try {
    if (!_context || !_context.globalState || typeof _context.globalState.update !== 'function') {
      return false;
    }
    await _context.globalState.update(PROFILES_DB_KEY, db || { version: 1, profiles: [] });
    return true;
  } catch (_) {
    return false;
  }
}

async function saveProfilesDbToSettings(db) {
  try {
    if (!_vscode || !_vscode.workspace || typeof _vscode.workspace.getConfiguration !== 'function') {
      return false;
    }
    const cfg = _vscode.workspace.getConfiguration('contextEngineUploader');
    if (!cfg || typeof cfg.update !== 'function') {
      return false;
    }
    const target = (_vscode.ConfigurationTarget && _vscode.ConfigurationTarget.Global !== undefined)
      ? _vscode.ConfigurationTarget.Global
      : true;
    await cfg.update(PROFILES_DB_SETTING_KEY, db || { version: 1, profiles: [] }, target);
    return true;
  } catch (_) {
    return false;
  }
}

function ensureGlobalStorageDir() {
  try {
    if (!_context || !_context.globalStorageUri || !_context.globalStorageUri.fsPath) {
      return false;
    }
    fs.mkdirSync(_context.globalStorageUri.fsPath, { recursive: true });
    return true;
  } catch (_) {
    return false;
  }
}

function getProfilesDbPath() {
  if (!_context || !_context.globalStorageUri || !_context.globalStorageUri.fsPath) {
    return undefined;
  }
  return path.join(_context.globalStorageUri.fsPath, PROFILES_DB_FILENAME);
}

function loadProfilesDb() {
  const fromSettings = loadProfilesDbFromSettings();
  if (fromSettings) {
    return fromSettings;
  }
  const mem = loadProfilesDbFromGlobalState();
  if (mem) {
    try {
      saveProfilesDbToSettings(mem).catch(() => { });
    } catch (_) {
    }
    return mem;
  }
  const dbPath = getProfilesDbPath();
  if (!dbPath) {
    return { version: 1, profiles: [] };
  }
  if (!fs.existsSync(dbPath)) {
    return { version: 1, profiles: [] };
  }
  try {
    const raw = fs.readFileSync(dbPath, 'utf8');
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && Array.isArray(parsed.profiles)) {
      const db = { version: 1, ...parsed };
      try {
        saveProfilesDbToGlobalState(db);
      } catch (_) {
      }
      try {
        saveProfilesDbToSettings(db).catch(() => { });
      } catch (_) {
      }
      return db;
    }
  } catch (_) {
  }
  return { version: 1, profiles: [] };
}

async function saveProfilesDb(db) {
  let saved = false;
  try {
    saved = (await saveProfilesDbToSettings(db)) || saved;
  } catch (_) {
  }
  try {
    saved = (await saveProfilesDbToGlobalState(db)) || saved;
  } catch (_) {
  }
  const dbPath = getProfilesDbPath();
  if (dbPath) {
    try {
      const payload = JSON.stringify(db || { version: 1, profiles: [] }, null, 2) + '\n';
      fs.writeFileSync(dbPath, payload, 'utf8');
      saved = true;
    } catch (_) {
    }
  }
  return saved;
}

function generateProfileId() {
  return `p_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

function getActiveProfileId() {
  try {
    if (_context && _context.workspaceState) {
      const raw = _context.workspaceState.get(ACTIVE_PROFILE_KEY);
      return typeof raw === 'string' ? raw : undefined;
    }
  } catch (_) {
  }
  try {
    if (_context && _context.globalState) {
      const raw = _context.globalState.get(ACTIVE_PROFILE_KEY);
      return typeof raw === 'string' ? raw : undefined;
    }
  } catch (_) {
  }
  return undefined;
}

async function setActiveProfileId(profileId) {
  try {
    if (_context && _context.workspaceState) {
      await _context.workspaceState.update(ACTIVE_PROFILE_KEY, profileId || undefined);
    }
  } catch (_) {
  }
  try {
    if (_context && _context.globalState && typeof _context.globalState.update === 'function') {
      await _context.globalState.update(ACTIVE_PROFILE_KEY, profileId || undefined);
    }
  } catch (_) {
  }
  try {
    _onProfileChanged();
  } catch (_) {
  }
}

function getActiveProfile() {
  const id = getActiveProfileId();
  if (!id) {
    return undefined;
  }
  const db = loadProfilesDb();
  const profiles = Array.isArray(db.profiles) ? db.profiles : [];
  return profiles.find(p => p && typeof p === 'object' && p.id === id);
}

function getActiveProfileSummary() {
  const p = getActiveProfile();
  if (!p) {
    return { id: undefined, name: undefined };
  }
  return {
    id: typeof p.id === 'string' ? p.id : undefined,
    name: typeof p.name === 'string' ? p.name : undefined,
  };
}

function listProfiles() {
  const db = loadProfilesDb();
  const profiles = Array.isArray(db.profiles) ? db.profiles : [];
  return profiles
    .filter(p => p && typeof p === 'object')
    .map(p => ({
      id: typeof p.id === 'string' ? p.id : '',
      name: typeof p.name === 'string' ? p.name : '',
    }))
    .filter(p => !!p.id)
    .sort((a, b) => {
      const an = (a.name || a.id).toLowerCase();
      const bn = (b.name || b.id).toLowerCase();
      if (an < bn) return -1;
      if (an > bn) return 1;
      return 0;
    });
}

function getActiveProfileOverrides() {
  const profile = getActiveProfile();
  if (!profile || typeof profile !== 'object') {
    return {};
  }
  const overrides = profile.overrides;
  if (!overrides || typeof overrides !== 'object') {
    return {};
  }
  return overrides;
}

async function updateActiveProfileOverride(key, value) {
  const activeId = getActiveProfileId();
  if (!activeId) {
    return false;
  }
  const db = loadProfilesDb();
  const profiles = Array.isArray(db.profiles) ? db.profiles : [];
  const profile = profiles.find(p => p && typeof p === 'object' && p.id === activeId);
  if (!profile) {
    return false;
  }
  if (!profile.overrides || typeof profile.overrides !== 'object') {
    profile.overrides = {};
  }
  profile.overrides[key] = value;
  profile.updatedAt = new Date().toISOString();
  db.profiles = profiles;
  const saved = await saveProfilesDb(db);
  if (saved) {
    try {
      _onProfileChanged();
    } catch (_) {
    }
  }
  return saved;
}

function getUploaderConfig() {
  if (!_vscode) {
    throw new Error('profiles.getUploaderConfig called before profiles.init');
  }
  const cfg = _vscode.workspace.getConfiguration('contextEngineUploader');
  const overrides = getActiveProfileOverrides();
  return {
    get: (key, defaultValue) => {
      if (Object.prototype.hasOwnProperty.call(overrides, key)) {
        return overrides[key];
      }
      return cfg.get(key, defaultValue);
    },
    inspect: (key) => {
      try {
        return typeof cfg.inspect === 'function' ? cfg.inspect(key) : undefined;
      } catch (_) {
        return undefined;
      }
    },
  };
}

function deepEqual(a, b) {
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch (_) {
    return a === b;
  }
}

function pickNonDefaultSetting(cfg, key) {
  if (!cfg || typeof cfg.get !== 'function') {
    return undefined;
  }
  const current = cfg.get(key);
  let inspected;
  try {
    inspected = typeof cfg.inspect === 'function' ? cfg.inspect(key) : undefined;
  } catch (_) {
    inspected = undefined;
  }
  const defaultValue = inspected && Object.prototype.hasOwnProperty.call(inspected, 'defaultValue') ? inspected.defaultValue : undefined;
  if (defaultValue !== undefined && deepEqual(current, defaultValue)) {
    return undefined;
  }
  if (current === undefined) {
    return undefined;
  }
  if (typeof current === 'string' && !current.trim()) {
    return undefined;
  }
  return current;
}

function normalizeProfileName(name) {
  return (name || '').trim();
}

async function setupWorkspaceWizard(deps) {
  if (!_vscode) {
    return;
  }
  try {
    ensureGlobalStorageDir();
  } catch (_) {
  }
  const rawCfg = _vscode.workspace.getConfiguration('contextEngineUploader');

  let inferredTarget = '';
  try {
    const res = deps && typeof deps.resolveTargetPathFromConfig === 'function' ? deps.resolveTargetPathFromConfig(rawCfg) : undefined;
    if (res && res.path) {
      inferredTarget = String(res.path);
    }
  } catch (_) {
  }

  try {
    if (!inferredTarget && deps && typeof deps.getWorkspaceFolderPath === 'function' && typeof deps.detectDefaultTargetPath === 'function') {
      const ws = deps.getWorkspaceFolderPath();
      if (ws) {
        inferredTarget = String(deps.detectDefaultTargetPath(ws) || '');
      }
    }
  } catch (_) {
  }

  const profileName = normalizeProfileName(await _vscode.window.showInputBox({
    prompt: 'Profile name (stored in VS Code extension storage; syncable via Settings Sync)',
    value: 'default',
    ignoreFocusOut: true,
  }));
  if (!profileName) {
    return;
  }

  const endpointDefault = (rawCfg.get('endpoint') || 'http://localhost:8004').trim();
  const endpoint = normalizeProfileName(await _vscode.window.showInputBox({
    prompt: 'Upload service endpoint (e.g. http://localhost:8004)',
    value: endpointDefault,
    ignoreFocusOut: true,
  }));
  if (!endpoint) {
    return;
  }

  const normalizeWorkspace = deps && typeof deps.normalizeWorkspaceForBridge === 'function'
    ? deps.normalizeWorkspaceForBridge
    : (p) => (p || '');

  const targetPath = normalizeProfileName(normalizeWorkspace(await _vscode.window.showInputBox({
    prompt: 'Target path to index/watch',
    value: inferredTarget,
    ignoreFocusOut: true,
  })));
  if (!targetPath) {
    return;
  }

  let healthOk = true;
  try {
    const fetchFn = deps && typeof deps.fetch === 'function' ? deps.fetch : (typeof fetch === 'function' ? fetch : undefined);
    if (fetchFn) {
      const base = endpoint.replace(/\/+$/, '');
      const res = await fetchFn(`${base}/health`, { method: 'GET' });
      healthOk = !!(res && res.ok);
    }
  } catch (_) {
    healthOk = false;
  }

  if (!healthOk) {
    const choice = await _vscode.window.showWarningMessage(
      'Context Engine Uploader: upload endpoint did not respond to /health. Save profile anyway?',
      'Save anyway',
      'Cancel',
    );
    if (choice !== 'Save anyway') {
      return;
    }
  }

  const serverMode = await _vscode.window.showQuickPick(
    ['bridge', 'direct'],
    { placeHolder: 'MCP server mode (optional)' },
  );
  const transportMode = await _vscode.window.showQuickPick(
    ['http', 'sse-remote'],
    { placeHolder: 'MCP transport mode (optional)' },
  );

  const overrides = {
    endpoint,
    targetPath,
  };
  if (serverMode) {
    overrides.mcpServerMode = serverMode;
  }
  if (transportMode) {
    overrides.mcpTransportMode = transportMode;
  }

  const db = loadProfilesDb();
  const profiles = Array.isArray(db.profiles) ? db.profiles : [];
  const existing = profiles.find(p => p && typeof p === 'object' && (p.name || '').trim() === profileName);
  const now = new Date().toISOString();

  let activeId;
  if (existing) {
    existing.overrides = { ...(existing.overrides || {}), ...overrides };
    existing.updatedAt = now;
    activeId = existing.id;
  } else {
    activeId = generateProfileId();
    profiles.push({
      id: activeId,
      name: profileName,
      createdAt: now,
      updatedAt: now,
      overrides,
    });
  }

  db.profiles = profiles;
  if (!await saveProfilesDb(db)) {
    _vscode.window.showErrorMessage('Context Engine Uploader: failed to save profiles database.');
    return;
  }

  await setActiveProfileId(activeId);
  _vscode.window.showInformationMessage(`Context Engine Uploader: active profile set to "${profileName}".`);

  const next = await _vscode.window.showQuickPick(
    ['Write MCP Config now', 'Write CTX Config now', 'Write MCP + CTX Config now', 'Index Codebase now', 'Done'],
    { placeHolder: 'Next step' },
  );
  if (!next) {
    return;
  }

  if ((next === 'Write MCP Config now' || next === 'Write MCP + CTX Config now') && deps && typeof deps.writeMcpConfig === 'function') {
    await deps.writeMcpConfig();
  }

  if ((next === 'Write CTX Config now' || next === 'Write MCP + CTX Config now') && deps && typeof deps.writeCtxConfig === 'function') {
    await deps.writeCtxConfig();
  }

  if (next === 'Index Codebase now' && deps && typeof deps.runSequence === 'function') {
    await deps.runSequence('force');
  }
}

async function switchProfileWizard() {
  if (!_vscode) {
    return;
  }
  const db = loadProfilesDb();
  const profiles = Array.isArray(db.profiles) ? db.profiles : [];
  const items = [{ label: 'None (use VS Code settings)', id: '' }]
    .concat(profiles
      .filter(p => p && typeof p === 'object')
      .map(p => ({ label: `${(p.name || p.id || '').trim() || 'Unnamed profile'}`, description: p.id, id: p.id })));
  const picked = await _vscode.window.showQuickPick(items, { placeHolder: 'Select active profile' });
  if (!picked) {
    return;
  }
  await setActiveProfileId(picked.id || undefined);
  if (!picked.id) {
    _vscode.window.showInformationMessage('Context Engine Uploader: using VS Code settings (no active profile).');
  } else {
    _vscode.window.showInformationMessage(`Context Engine Uploader: switched active profile to "${picked.label}".`);
  }
}

async function createProfileFromCurrentSettingsWizard() {
  if (!_vscode) {
    return;
  }
  try {
    ensureGlobalStorageDir();
  } catch (_) {
  }
  const cfg = _vscode.workspace.getConfiguration('contextEngineUploader');
  const name = normalizeProfileName(await _vscode.window.showInputBox({
    prompt: 'Profile name',
    ignoreFocusOut: true,
  }));
  if (!name) {
    return;
  }

  const keys = [
    'endpoint',
    'targetPath',
    'hostRoot',
    'containerRoot',
    'pythonPath',
    'intervalSeconds',
    'extraForceArgs',
    'extraWatchArgs',
    'startWatchAfterForce',
    'mcpClaudeEnabled',
    'mcpWindsurfEnabled',
    'autoWriteMcpConfigOnStartup',
    'mcpTransportMode',
    'mcpServerMode',
    'mcpIndexerUrl',
    'mcpMemoryUrl',
    'autoStartMcpBridge',
    'mcpBridgePort',
    'mcpBridgeBinPath',
    'mcpBridgeLocalOnly',
    'claudeHookEnabled',
    'surfaceQdrantCollectionHint',
    'devRemoteMode',
    'scaffoldCtxConfig',
    'decoderRuntime',
    'decoderUrl',
    'useGpuDecoder',
    'ctxIndexerUrl',
    'gitMaxCommits',
    'gitSince',
  ];

  const overrides = {};
  for (const key of keys) {
    const value = pickNonDefaultSetting(cfg, key);
    if (value !== undefined) {
      overrides[key] = value;
    }
  }

  const now = new Date().toISOString();
  const db = loadProfilesDb();
  const profiles = Array.isArray(db.profiles) ? db.profiles : [];
  const id = generateProfileId();
  profiles.push({
    id,
    name,
    createdAt: now,
    updatedAt: now,
    overrides,
  });
  db.profiles = profiles;
  if (!await saveProfilesDb(db)) {
    _vscode.window.showErrorMessage('Context Engine Uploader: failed to save profiles database.');
    return;
  }
  await setActiveProfileId(id);
  _vscode.window.showInformationMessage(`Context Engine Uploader: created and activated profile "${name}".`);
}

async function exportProfilesWizard() {
  if (!_vscode) {
    return;
  }
  const db = loadProfilesDb();
  const uri = await _vscode.window.showSaveDialog({
    title: 'Export Context Engine Uploader profiles',
    filters: { JSON: ['json'] },
    saveLabel: 'Export',
  });
  if (!uri) {
    return;
  }
  try {
    const payload = JSON.stringify(db || { version: 1, profiles: [] }, null, 2) + '\n';
    await _vscode.workspace.fs.writeFile(uri, Buffer.from(payload, 'utf8'));
    _vscode.window.showInformationMessage('Context Engine Uploader: profiles exported.');
  } catch (error) {
    _vscode.window.showErrorMessage('Context Engine Uploader: failed to export profiles.');
    _log(`Export profiles failed: ${error instanceof Error ? error.message : String(error)}`);
  }
}

async function importProfilesWizard() {
  if (!_vscode) {
    return;
  }
  try {
    ensureGlobalStorageDir();
  } catch (_) {
  }
  const uris = await _vscode.window.showOpenDialog({
    title: 'Import Context Engine Uploader profiles',
    canSelectMany: false,
    filters: { JSON: ['json'] },
  });
  if (!uris || !uris.length) {
    return;
  }

  let parsed;
  try {
    const bytes = await _vscode.workspace.fs.readFile(uris[0]);
    parsed = JSON.parse(Buffer.from(bytes).toString('utf8'));
  } catch (error) {
    _vscode.window.showErrorMessage('Context Engine Uploader: invalid profiles JSON.');
    _log(`Import profiles parse failed: ${error instanceof Error ? error.message : String(error)}`);
    return;
  }

  const incoming = parsed && typeof parsed === 'object' && Array.isArray(parsed.profiles) ? parsed.profiles : [];
  if (!incoming.length) {
    _vscode.window.showWarningMessage('Context Engine Uploader: no profiles found in import file.');
    return;
  }

  const db = loadProfilesDb();
  const profiles = Array.isArray(db.profiles) ? db.profiles : [];

  for (const p of incoming) {
    if (!p || typeof p !== 'object') {
      continue;
    }
    const pid = typeof p.id === 'string' ? p.id : '';
    const pname = typeof p.name === 'string' ? p.name.trim() : '';
    if (!pid && !pname) {
      continue;
    }
    const existing = pid
      ? profiles.find(x => x && typeof x === 'object' && x.id === pid)
      : profiles.find(x => x && typeof x === 'object' && (x.name || '').trim() === pname);

    if (existing) {
      existing.name = pname || existing.name;
      existing.overrides = { ...(existing.overrides || {}), ...(p.overrides || {}) };
      existing.updatedAt = new Date().toISOString();
    } else {
      profiles.push({
        id: pid || generateProfileId(),
        name: pname || 'Imported Profile',
        createdAt: p.createdAt || new Date().toISOString(),
        updatedAt: new Date().toISOString(),
        overrides: (p.overrides && typeof p.overrides === 'object') ? p.overrides : {},
      });
    }
  }

  db.profiles = profiles;
  if (!await saveProfilesDb(db)) {
    _vscode.window.showErrorMessage('Context Engine Uploader: failed to save profiles database after import.');
    return;
  }

  _vscode.window.showInformationMessage(`Context Engine Uploader: imported ${incoming.length} profile(s).`);
}

async function deleteProfileWizard(profileIdFromContext) {
  if (!_vscode) {
    return;
  }
  const db = loadProfilesDb();
  const profiles = Array.isArray(db.profiles) ? db.profiles : [];
  if (!profiles.length) {
    _vscode.window.showInformationMessage('Context Engine: no profiles to delete.');
    return;
  }

  const activeId = getActiveProfileId();
  let targetId = profileIdFromContext;
  let targetLabel;

  if (targetId) {
    const targetProfile = profiles.find(p => p && p.id === targetId);
    if (targetProfile) {
      targetLabel = targetProfile.name || targetProfile.id;
    }
  }

  if (!targetId) {
    const items = profiles
      .filter(p => p && typeof p === 'object' && p.id)
      .map(p => ({
        label: `${(p.name || p.id || '').trim() || 'Unnamed profile'}${p.id === activeId ? ' (active)' : ''}`,
        description: p.id,
        id: p.id,
      }));

    const picked = await _vscode.window.showQuickPick(items, {
      placeHolder: 'Select profile to delete',
      canPickMany: false,
    });
    if (!picked) {
      return;
    }
    targetId = picked.id;
    targetLabel = picked.label;
  }

  const confirm = await _vscode.window.showWarningMessage(
    `Delete profile "${targetLabel}"?`,
    { modal: true },
    'Delete',
  );
  if (confirm !== 'Delete') {
    return;
  }

  db.profiles = profiles.filter(p => p && p.id !== targetId);

  if (!await saveProfilesDb(db)) {
    _vscode.window.showErrorMessage('Context Engine: failed to save profiles database after delete.');
    return;
  }

  if (activeId === targetId) {
    await setActiveProfileId(undefined);
    _vscode.window.showInformationMessage(`Context Engine: deleted profile "${targetLabel}" and cleared active profile.`);
  } else {
    _vscode.window.showInformationMessage(`Context Engine: deleted profile "${targetLabel}".`);
  }
}

async function renameProfileWizard(profileIdFromContext) {
  if (!_vscode) {
    return;
  }
  const db = loadProfilesDb();
  const profiles = Array.isArray(db.profiles) ? db.profiles : [];
  if (!profiles.length) {
    _vscode.window.showInformationMessage('Context Engine: no profiles to rename.');
    return;
  }

  let targetProfile;
  if (profileIdFromContext) {
    targetProfile = profiles.find(p => p && p.id === profileIdFromContext);
  }

  if (!targetProfile) {
    const items = profiles.map(p => ({
      label: p.name || p.id,
      description: p.id,
      id: p.id,
    }));

    const picked = await _vscode.window.showQuickPick(items, {
      placeHolder: 'Select profile to rename',
      canPickMany: false,
    });
    if (!picked) {
      return;
    }
    targetProfile = profiles.find(p => p && p.id === picked.id);
  }

  if (!targetProfile) {
    return;
  }

  const newName = await _vscode.window.showInputBox({
    prompt: 'Enter new name for the profile',
    value: targetProfile.name || targetProfile.id,
    validateInput: (value) => {
      if (!value || !value.trim()) {
        return 'Name cannot be empty';
      }
      return undefined;
    },
  });

  if (!newName) {
    return;
  }

  targetProfile.name = newName.trim();

  if (!await saveProfilesDb(db)) {
    _vscode.window.showErrorMessage('Context Engine: failed to save profiles database after rename.');
    return;
  }

  _vscode.window.showInformationMessage(`Context Engine: renamed profile to "${newName.trim()}".`);
}

async function duplicateProfileWizard(profileIdFromContext) {
  if (!_vscode) {
    return;
  }
  const db = loadProfilesDb();
  const profiles = Array.isArray(db.profiles) ? db.profiles : [];
  if (!profiles.length) {
    _vscode.window.showInformationMessage('Context Engine: no profiles to duplicate.');
    return;
  }

  let sourceProfile;
  if (profileIdFromContext) {
    sourceProfile = profiles.find(p => p && p.id === profileIdFromContext);
  }

  if (!sourceProfile) {
    const items = profiles.map(p => ({
      label: p.name || p.id,
      description: p.id,
      id: p.id,
    }));

    const picked = await _vscode.window.showQuickPick(items, {
      placeHolder: 'Select profile to duplicate',
      canPickMany: false,
    });
    if (!picked) {
      return;
    }
    sourceProfile = profiles.find(p => p && p.id === picked.id);
  }

  if (!sourceProfile) {
    return;
  }

  const newName = await _vscode.window.showInputBox({
    prompt: 'Enter name for the duplicated profile',
    value: `${sourceProfile.name || sourceProfile.id} (Copy)`,
    validateInput: (value) => {
      if (!value || !value.trim()) {
        return 'Name cannot be empty';
      }
      return undefined;
    },
  });

  if (!newName) {
    return;
  }

  const newId = `p_${Math.random().toString(36).slice(2, 12)}_${Date.now().toString(36)}`;
  const newProfile = {
    ...JSON.parse(JSON.stringify(sourceProfile)),
    id: newId,
    name: newName.trim(),
    createdAt: Date.now(),
  };

  db.profiles.push(newProfile);

  if (!await saveProfilesDb(db)) {
    _vscode.window.showErrorMessage('Context Engine: failed to save profiles database after duplicate.');
    return;
  }

  _vscode.window.showInformationMessage(`Context Engine: created duplicate profile "${newName.trim()}".`);
}

function registerCommands(deps) {
  if (!_vscode || !_context) {
    throw new Error('profiles.registerCommands called before profiles.init');
  }
  return [
    _vscode.commands.registerCommand('contextEngineUploader.setupWorkspace', () => setupWorkspaceWizard(deps)),
    _vscode.commands.registerCommand('contextEngineUploader.switchProfile', () => switchProfileWizard()),
    _vscode.commands.registerCommand('contextEngineUploader.createProfileFromCurrentSettings', () => createProfileFromCurrentSettingsWizard()),
    _vscode.commands.registerCommand('contextEngineUploader.exportProfiles', () => exportProfilesWizard()),
    _vscode.commands.registerCommand('contextEngineUploader.importProfiles', () => importProfilesWizard()),
    _vscode.commands.registerCommand('contextEngineUploader.deleteProfile', (item) => deleteProfileWizard(item && item.id)),
    _vscode.commands.registerCommand('contextEngineUploader.renameProfile', (item) => renameProfileWizard(item && item.id)),
    _vscode.commands.registerCommand('contextEngineUploader.duplicateProfile', (item) => duplicateProfileWizard(item && item.id)),
  ];
}

module.exports = {
  init,
  getUploaderConfig,
  listProfiles,
  getActiveProfileSummary,
  setActiveProfileId,
  getActiveProfileOverrides,
  updateActiveProfileOverride,
  registerCommands,
};
