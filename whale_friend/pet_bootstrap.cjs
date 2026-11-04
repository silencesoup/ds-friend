'use strict';
// Adapter for the MIT-licensed dsh-pet renderer. No DSH profile or credential access.
const {app, ipcMain} = require('electron');
const path = require('node:path');
const http = require('node:http');
app.setPath('userData', process.env.DS_FRIEND_PET_PROFILE);
const on = ipcMain.on.bind(ipcMain);
ipcMain.on = function(channel, listener) {
  if (channel === 'pet:open-site' || channel === 'pet:focus-host') {
    return on(channel, () => process.stdout.write('ds-friend-pet:' + JSON.stringify({kind:'open'}) + '\n'));
  }
  return on(channel, listener);
};
const control = http.createServer((request, response) => {
  if (request.method !== 'POST' || request.url !== '/shutdown' || request.headers.authorization !== 'Bearer ' + process.env.DS_FRIEND_PET_CONTROL) {
    response.writeHead(404); response.end(); return;
  }
  response.end('ok'); setImmediate(() => app.quit());
});
control.listen(0, '127.0.0.1', () => {
  process.stdout.write('ds-friend-pet:' + JSON.stringify({kind:'control', port:control.address().port}) + '\n');
});
control.unref();
require(path.join(__dirname, 'helper', 'main.js'));
