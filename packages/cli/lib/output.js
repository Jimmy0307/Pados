'use strict';
// Human-readable rendering of PADOS results (the --json form prints the underlying documents unchanged).

const LABEL = { PASS: '[PASS]   ', WARNING: '[WARNING]', BLOCKED: '[BLOCKED]' };

function write(text = '') {
  process.stdout.write(`${text}\n`);
}

function renderDoctor(report) {
  const lines = [`PADOS doctor: ${report.status}   (PADOS_HOME ${report.home})`];
  for (const area of report.areas) {
    lines.push(`${LABEL[area.status] || area.status} ${area.area.padEnd(14)} ${area.summary}`);
    if (area.action && area.status !== 'PASS') lines.push(`${' '.repeat(25)}-> ${area.action}`);
  }
  return lines.join('\n');
}

function humanBytes(bytes) {
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let value = Number(bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

module.exports = { write, renderDoctor, humanBytes };
