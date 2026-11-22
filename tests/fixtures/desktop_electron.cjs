const { app, BrowserWindow } = require('electron');
const path = require('node:path');

const profile = process.argv[2];
if (!profile || !path.isAbsolute(profile)) throw new Error('Provide a temporary profile path.');
app.setPath('userData', profile);
app.commandLine.appendSwitch('disable-background-networking');
app.commandLine.appendSwitch('enable-features', 'UiaProvider');
app.commandLine.appendSwitch('force-renderer-accessibility', 'complete');

app.whenReady().then(async () => {
    app.setAccessibilitySupportEnabled(true);
    const window = new BrowserWindow({
        width: 640,
        height: 420,
        webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true },
    });
    await window.loadFile(path.join(__dirname, 'desktop_webgl.html'));
}).catch((error) => {
    console.error(error);
    app.exit(1);
});

app.on('window-all-closed', () => app.quit());
