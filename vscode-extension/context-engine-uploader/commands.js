/**
 * Command registration for Context Engine extension.
 * Consolidates all vscode.commands.registerCommand calls.
 */
function registerExtensionCommands(deps) {
    if (!deps || typeof deps !== 'object') {
        throw new Error('registerExtensionCommands: deps object is required');
    }

    const vscode = deps.vscode;
    const log = deps.log;

    // Verify critical dependencies immediately
    if (!vscode) {
        throw new Error('Context Engine Uploader: vscode dependency is missing (extension failed to initialize).');
    }
    if (!log) {
        throw new Error('Context Engine Uploader: log dependency is missing (extension failed to initialize).');
    }

    const getEffectiveConfig = deps.getEffectiveConfig;
    const getOutputChannel = deps.getOutputChannel;
    const runSequence = deps.runSequence;
    const stopProcesses = deps.stopProcesses;
    const writeMcpConfig = deps.writeMcpConfig;
    const writeCtxConfig = deps.writeCtxConfig;
    const startHttpBridgeProcess = deps.startHttpBridgeProcess;
    const stopHttpBridgeProcess = deps.stopHttpBridgeProcess;
    const buildAuthDeps = deps.buildAuthDeps;
    const runAuthLoginFlow = deps.runAuthLoginFlow;
    const runAuthLogoutFlow = deps.runAuthLogoutFlow;
    const getOnboardingManager = deps.getOnboardingManager;
    const getLogsTerminalManager = deps.getLogsTerminalManager;
    const getBridgeManager = deps.getBridgeManager;

    const disposables = [];

    const handleCatch = (error, prefix) => {
        const msg = error instanceof Error ? error.message : String(error);
        log(`${prefix}: ${msg}`);
        vscode.window.showErrorMessage(`Context Engine Uploader: ${prefix}: ${msg}`);
    };

    const requireDep = (value, name) => {
        if (value === undefined || value === null) {
            throw new Error(`Context Engine Uploader: ${name} is unavailable (extension failed to initialize this component).`);
        }
        return value;
    };

    const resolveEndpointOrThrow = () => {
        const getEffectiveConfigFn = requireDep(getEffectiveConfig, 'getEffectiveConfig');
        if (typeof getEffectiveConfigFn !== 'function') {
            throw new Error('getEffectiveConfig is not available (not a function).');
        }
        const cfg = getEffectiveConfigFn();
        const endpoint = (cfg.get('endpoint') || '').trim();
        if (!endpoint) {
            throw new Error('backend endpoint is not configured (contextEngineUploader.endpoint).');
        }
        return endpoint;
    };

    // Start/Stop/Restart commands
    disposables.push(vscode.commands.registerCommand('contextEngineUploader.start', () => {
        try {
            requireDep(runSequence, 'runSequence')('auto').catch(error => handleCatch(error, 'Start failed'));
        } catch (error) {
            handleCatch(error, 'Start failed');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.stop', () => {
        try {
            requireDep(stopProcesses, 'stopProcesses')().catch(error => handleCatch(error, 'Stop failed'));
        } catch (error) {
            handleCatch(error, 'Stop failed');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.restart', () => {
        try {
            requireDep(stopProcesses, 'stopProcesses')()
                .then(() => requireDep(runSequence, 'runSequence')('auto'))
                .catch(error => handleCatch(error, 'Restart failed'));
        } catch (error) {
            handleCatch(error, 'Restart failed');
        }
    }));

    // Index commands
    disposables.push(vscode.commands.registerCommand('contextEngineUploader.indexCodebase', () => {
        vscode.window.showInformationMessage('Context Engine indexing started.');
        const outputChannel = getOutputChannel();
        if (outputChannel) { outputChannel.show(true); }
        try {
            requireDep(runSequence, 'runSequence')('force').catch(error => handleCatch(error, 'Index failed'));
        } catch (error) {
            handleCatch(error, 'Index failed');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.uploadGitHistory', () => {
        vscode.window.showInformationMessage('Context Engine git history upload (force sync bundle) started.');
        const outputChannel = getOutputChannel();
        if (outputChannel) { outputChannel.show(true); }
        try {
            requireDep(runSequence, 'runSequence')('uploadGitHistory').catch(error => handleCatch(error, 'Git history upload failed'));
        } catch (error) {
            handleCatch(error, 'Git history upload failed');
        }
    }));

    // Config commands
    disposables.push(vscode.commands.registerCommand('contextEngineUploader.writeCtxConfig', () => {
        try {
            requireDep(writeCtxConfig, 'writeCtxConfig')().catch(error => handleCatch(error, 'Failed to write CTX config'));
        } catch (error) {
            handleCatch(error, 'Failed to write CTX config');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.writeMcpConfig', () => {
        try {
            requireDep(writeMcpConfig, 'writeMcpConfig')().catch(error => handleCatch(error, 'Failed to write MCP config'));
        } catch (error) {
            handleCatch(error, 'Failed to write MCP config');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.writeMcpConfigSelect', async () => {
        try {
            const getEffectiveConfigFn = requireDep(getEffectiveConfig, 'getEffectiveConfig');
            if (typeof getEffectiveConfigFn !== 'function') {
                throw new Error('getEffectiveConfig is not available (not a function).');
            }
            const cfg = getEffectiveConfigFn();
            const claudeEnabled = !!cfg.get('mcpClaudeEnabled', true);
            const windsurfEnabled = !!cfg.get('mcpWindsurfEnabled', false);
            const augmentEnabled = !!cfg.get('mcpAugmentEnabled', false);
            const antigravityEnabled = !!cfg.get('mcpAntigravityEnabled', false);

            const items = [
                {
                    label: 'All enabled targets',
                    description: 'Writes MCP config for all enabled clients',
                    id: 'all',
                },
                {
                    label: 'Claude Code (.mcp.json)',
                    description: claudeEnabled ? 'Enabled' : 'Disabled in settings',
                    id: 'claude',
                },
                {
                    label: 'Windsurf (mcp_config.json)',
                    description: windsurfEnabled ? 'Enabled' : 'Disabled in settings',
                    id: 'windsurf',
                },
                {
                    label: 'Augment Code (~/.augment/settings.json)',
                    description: augmentEnabled ? 'Enabled' : 'Disabled in settings',
                    id: 'augment',
                },
                {
                    label: 'Antigravity (~/.gemini/antigravity/mcp_config.json)',
                    description: antigravityEnabled ? 'Enabled' : 'Disabled in settings',
                    id: 'antigravity',
                },
            ];

            const picked = await vscode.window.showQuickPick(items, { placeHolder: 'Select which MCP config to write' });
            if (!picked) {
                return;
            }

            if (picked.id === 'all') {
                await requireDep(writeMcpConfig, 'writeMcpConfig')();
            } else if (picked.id === 'claude') {
                await requireDep(writeMcpConfig, 'writeMcpConfig')({ targets: ['claude'] });
            } else if (picked.id === 'windsurf') {
                await requireDep(writeMcpConfig, 'writeMcpConfig')({ targets: ['windsurf'] });
            } else if (picked.id === 'augment') {
                await requireDep(writeMcpConfig, 'writeMcpConfig')({ targets: ['augment'] });
            } else if (picked.id === 'antigravity') {
                await requireDep(writeMcpConfig, 'writeMcpConfig')({ targets: ['antigravity'] });
            }
        } catch (error) {
            handleCatch(error, 'MCP config select failed');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.writeMcpConfigClaude', () => {
        try {
            requireDep(writeMcpConfig, 'writeMcpConfig')({ targets: ['claude'] }).catch(error => handleCatch(error, 'Failed to write Claude MCP config'));
        } catch (error) {
            handleCatch(error, 'Failed to write Claude MCP config');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.writeMcpConfigWindsurf', () => {
        try {
            requireDep(writeMcpConfig, 'writeMcpConfig')({ targets: ['windsurf'] }).catch(error => handleCatch(error, 'Failed to write Windsurf MCP config'));
        } catch (error) {
            handleCatch(error, 'Failed to write Windsurf MCP config');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.writeMcpConfigAugment', () => {
        try {
            requireDep(writeMcpConfig, 'writeMcpConfig')({ targets: ['augment'] }).catch(error => handleCatch(error, 'Failed to write Augment MCP config'));
        } catch (error) {
            handleCatch(error, 'Failed to write Augment MCP config');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.writeMcpConfigAntigravity', () => {
        try {
            requireDep(writeMcpConfig, 'writeMcpConfig')({ targets: ['antigravity'] }).catch(error => handleCatch(error, 'Failed to write Antigravity MCP config'));
        } catch (error) {
            handleCatch(error, 'Failed to write Antigravity MCP config');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.writeMcpConfigCursor', () => {
        try {
            requireDep(writeMcpConfig, 'writeMcpConfig')({ targets: ['cursor'] }).catch(error => handleCatch(error, 'Failed to write Cursor MCP config'));
        } catch (error) {
            handleCatch(error, 'Failed to write Cursor MCP config');
        }
    }));

    // Onboarding/Stack commands
    disposables.push(vscode.commands.registerCommand('contextEngineUploader.cloneAndStartStack', async () => {
        try {
            const onboardingManager = requireDep(getOnboardingManager, 'getOnboardingManager')();
            if (!onboardingManager || typeof onboardingManager.cloneAndStartStack !== 'function') {
                throw new Error('Context Engine onboarding is unavailable in this session.');
            }
            await onboardingManager.cloneAndStartStack();
        } catch (error) {
            handleCatch(error, 'Clone and start stack failed');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.startSavedStack', async () => {
        try {
            const onboardingManager = requireDep(getOnboardingManager, 'getOnboardingManager')();
            if (!onboardingManager || typeof onboardingManager.startSavedStack !== 'function') {
                throw new Error('Context Engine onboarding is unavailable in this session.');
            }
            await onboardingManager.startSavedStack();
        } catch (error) {
            handleCatch(error, 'Start saved stack failed');
        }
    }));

    // Logs commands
    disposables.push(vscode.commands.registerCommand('contextEngineUploader.showUploadServiceLogs', () => {
        try {
            const outputChannel = getOutputChannel();
            if (outputChannel) {
                outputChannel.show(true);
            } else {
                throw new Error('output channel is unavailable');
            }
        } catch (e) {
            handleCatch(e, 'Show logs failed');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.tailUploadServiceLogs', () => {
        try {
            const logsTerminalManager = requireDep(getLogsTerminalManager, 'getLogsTerminalManager')();
            if (logsTerminalManager && typeof logsTerminalManager.open === 'function') {
                logsTerminalManager.open();
            } else {
                throw new Error('log tailing is unavailable (extension failed to initialize logs terminal manager)');
            }
        } catch (e) {
            handleCatch(e, 'Tail logs failed');
        }
    }));

    // Bridge commands
    disposables.push(vscode.commands.registerCommand('contextEngineUploader.startMcpHttpBridge', () => {
        try {
            const cfg = requireDep(getEffectiveConfig, 'getEffectiveConfig')();
            const serverMode = (cfg.get('mcpServerMode') || 'bridge').trim();
            const transportMode = (cfg.get('mcpTransportMode') || 'sse-remote').trim();
            if (serverMode === 'direct') {
                vscode.window.showWarningMessage('Context Engine Uploader: Server Mode is "direct" - the HTTP bridge is not needed. The bridge is only used when Server Mode is "bridge" and Transport is "http".');
                return;
            }
            if (serverMode === 'bridge' && transportMode !== 'http') {
                vscode.window.showInformationMessage(`Context Engine Uploader: Starting HTTP bridge, but Transport Mode is "${transportMode}". The bridge is typically used with Transport Mode "http".`);
            }
            requireDep(startHttpBridgeProcess, 'startHttpBridgeProcess')().catch(error => handleCatch(error, 'HTTP MCP bridge start failed'));
        } catch (error) {
            handleCatch(error, 'HTTP MCP bridge start failed');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.stopMcpHttpBridge', () => {
        try {
            requireDep(stopHttpBridgeProcess, 'stopHttpBridgeProcess')().catch(error => handleCatch(error, 'HTTP MCP bridge stop failed'));
        } catch (error) {
            handleCatch(error, 'HTTP MCP bridge stop failed');
        }
    }));

    // Auth commands
    disposables.push(vscode.commands.registerCommand('contextEngineUploader.authLogin', async () => {
        try {
            const endpoint = resolveEndpointOrThrow();
            await requireDep(runAuthLoginFlow, 'runAuthLoginFlow')(endpoint, requireDep(buildAuthDeps, 'buildAuthDeps')());
        } catch (error) {
            handleCatch(error, 'Auth login failed');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.authLogout', async () => {
        try {
            const endpoint = resolveEndpointOrThrow();
            await requireDep(runAuthLogoutFlow, 'runAuthLogoutFlow')(endpoint, requireDep(buildAuthDeps, 'buildAuthDeps')());
        } catch (error) {
            handleCatch(error, 'Auth logout failed');
        }
    }));

    disposables.push(vscode.commands.registerCommand('contextEngineUploader.graphBackfill', async () => {
        try {
            const cfg = requireDep(getEffectiveConfig, 'getEffectiveConfig')();
            const serverMode = (cfg.get('mcpServerMode') || 'bridge').trim();
            const transportMode = (cfg.get('mcpTransportMode') || 'sse-remote').trim();

            let mcpUrl = '';
            if (serverMode === 'bridge' && transportMode === 'http') {
                const bridgeManager = requireDep(getBridgeManager, 'getBridgeManager')?.();
                mcpUrl = bridgeManager ? bridgeManager.resolveBridgeHttpUrl() : '';
            }
            if (!mcpUrl) {
                mcpUrl = (cfg.get('ctxIndexerUrl') || cfg.get('mcpIndexerUrl') || 'http://localhost:8003/mcp').trim();
            }

            if (!mcpUrl) {
                vscode.window.showErrorMessage('Context Engine Uploader: MCP server URL not configured');
                return;
            }

            const outputChannel = getOutputChannel();
            if (outputChannel) { outputChannel.show(true); }

            log('Detecting collection name...');

            let collectionName = '';
            try {
                const listResponse = await fetch(mcpUrl, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json, text/event-stream'
                    },
                    body: JSON.stringify({
                        jsonrpc: '2.0',
                        id: Date.now(),
                        method: 'tools/call',
                        params: {
                            name: 'qdrant_list',
                            arguments: {}
                        }
                    })
                });

                if (listResponse.ok) {
                    const listText = await listResponse.text();
                    let listResult;
                    if (listText.includes('event:') || listText.includes('data:')) {
                        const lines = listText.split('\n');
                        let lastDataLine = null;
                        for (const line of lines) {
                            if (line.startsWith('data:')) {
                                lastDataLine = line.substring(5).trim();
                            }
                        }
                        if (lastDataLine) {
                            listResult = JSON.parse(lastDataLine);
                        }
                    } else {
                        listResult = JSON.parse(listText);
                    }

                    let data = listResult.result || listResult;
                    if (data.structuredContent && data.structuredContent.result) {
                        data = data.structuredContent.result;
                    } else if (data.content && Array.isArray(data.content) && data.content[0] && data.content[0].text) {
                        try {
                            data = JSON.parse(data.content[0].text);
                        } catch (e) {
                            log(`Failed to parse qdrant_list content: ${e instanceof Error ? e.message : String(e)}`);
                        }
                    }

                    const collections = data.collections || [];
                    for (const coll of collections) {
                        if (coll && !coll.startsWith('models-') && coll !== 'codebase' && !coll.endsWith('_graph')) {
                            collectionName = coll;
                            break;
                        }
                    }
                    if (collectionName) {
                        log(`Detected collection: ${collectionName}`);
                    }
                }
            } catch (e) {
                log(`Failed to detect collection: ${e instanceof Error ? e.message : String(e)}`);
            }

            log('Starting graph backfill...');

            const maxIterations = 50;
            const totalResult = {
                processed: 0,
                iterations: 0,
                complete: false,
            };

            for (let i = 0; i < maxIterations; i++) {
                try {
                    const backfillArgs = {
                        max_points: 1000,
                        max_iterations: 1
                    };
                    if (collectionName) {
                        backfillArgs.collection = collectionName;
                    }

                    const response = await fetch(mcpUrl, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'Accept': 'application/json, text/event-stream'
                        },
                        body: JSON.stringify({
                            jsonrpc: '2.0',
                            id: Date.now(),
                            method: 'tools/call',
                            params: {
                                name: 'graph_backfill',
                                arguments: backfillArgs
                            }
                        })
                    });

                    if (!response.ok) {
                        log(`Graph backfill iteration ${i + 1} failed: ${response.status}`);
                        break;
                    }

                    const text = await response.text();
                    let result;

                    try {
                        if (text.includes('event:') || text.includes('data:')) {
                            const lines = text.split('\n');
                            let lastDataLine = null;
                            for (const line of lines) {
                                if (line.startsWith('data:')) {
                                    lastDataLine = line.substring(5).trim();
                                }
                            }
                            if (lastDataLine) {
                                result = JSON.parse(lastDataLine);
                            } else {
                                result = JSON.parse(text);
                            }
                        } else {
                            result = JSON.parse(text);
                        }
                    } catch (e) {
                        log(`Failed to parse response: ${e instanceof Error ? e.message : String(e)}`);
                        log(`Response text: ${text.substring(0, 500)}`);
                        break;
                    }

                    if (result.error) {
                        log(`Graph backfill error: ${JSON.stringify(result.error)}`);
                        break;
                    }

                    let data = result.result || result;
                    if (data.structuredContent && data.structuredContent.result) {
                        data = data.structuredContent.result;
                    } else if (data.content && Array.isArray(data.content) && data.content[0] && data.content[0].text) {
                        try {
                            data = JSON.parse(data.content[0].text);
                        } catch (e) {
                            log(`Failed to parse content text: ${e instanceof Error ? e.message : String(e)}`);
                        }
                    }
                    const processed = data.processed || 0;
                    const complete = data.complete !== undefined ? data.complete : (processed === 0);

                    totalResult.processed += processed;
                    totalResult.iterations++;

                    log(`Graph backfill iteration ${i + 1}: processed ${processed} points (total: ${totalResult.processed})`);

                    if (processed === 0 || complete) {
                        totalResult.complete = true;
                        break;
                    }

                } catch (error) {
                    log(`Graph backfill iteration ${i + 1} error: ${error instanceof Error ? error.message : String(error)}`);
                    break;
                }
            }

            if (totalResult.complete) {
                vscode.window.showInformationMessage(`Graph backfill complete: ${totalResult.processed} points in ${totalResult.iterations} iterations`);
            } else {
                vscode.window.showWarningMessage(`Graph backfill incomplete: ${totalResult.processed} points in ${totalResult.iterations} iterations (may need more iterations)`);
            }

        } catch (error) {
            handleCatch(error, 'Graph backfill failed');
        }
    }));

    return disposables;
}

module.exports = {
    registerExtensionCommands,
};
