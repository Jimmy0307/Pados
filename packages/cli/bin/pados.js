#!/usr/bin/env node
'use strict';

const major = Number(process.versions.node.split('.')[0]);
if (major < 20) {
  process.stderr.write(`PADOS needs Node.js 20 or newer (found ${process.version}).\n`);
  process.exit(1);
}

require('../lib/main').main().then((code) => {
  process.exitCode = code;
});
