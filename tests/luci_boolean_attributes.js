// Host-only audit: npm --prefix ui ci, then node tests/luci_boolean_attributes.js [source roots...]
// LuCI E() writes non-null attributes with setAttribute(), unlike React or DOM properties.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('../ui/node_modules/typescript');

const booleans = new Set(('disabled checked selected hidden readonly multiple required autofocus ' +
    'controls autoplay loop muted open reversed novalidate formnovalidate allowfullscreen inert').split(' '));

function mayDisableByMistake(node) {
    if (ts.isParenthesizedExpression(node)) return mayDisableByMistake(node.expression);
    if (node.kind === ts.SyntaxKind.NullKeyword || node.kind === ts.SyntaxKind.TrueKeyword) return false;
    if (ts.isIdentifier(node) && node.text === 'undefined') return false;
    if (ts.isStringLiteral(node)) return node.text === 'false' || node.text === '0';
    if (ts.isConditionalExpression(node)) return mayDisableByMistake(node.whenTrue) || mayDisableByMistake(node.whenFalse);
    // Canonical LuCI form: condition || null. Other logical expressions can return false.
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.BarBarToken)
        return mayDisableByMistake(node.right);
    return true;
}

function inspect(source, filename) {
    const ast = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true);
    const issues = [];
    function report(node, message) {
        issues.push(filename + ':' + (ast.getLineAndCharacterOfPosition(node.getStart(ast)).line + 1) + ': ' + message);
    }
    function visit(node) {
        if (ts.isCallExpression(node)) {
            const factory = node.expression.getText(ast);
            if (['E', 'dom.create', 'L.dom.create'].includes(factory) && node.arguments[1] && ts.isObjectLiteralExpression(node.arguments[1])) {
                for (const prop of node.arguments[1].properties) {
                    if (ts.isPropertyAssignment(prop) && booleans.has(prop.name.text) && mayDisableByMistake(prop.initializer))
                        report(prop, prop.name.text + ' may serialize a false boolean; omit it with null');
                }
            }
            if (ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === 'setAttribute' &&
                booleans.has(node.arguments[0]?.text) && node.arguments[1] && mayDisableByMistake(node.arguments[1]))
                report(node, 'use the DOM boolean property or removeAttribute() instead');
        }
        ts.forEachChild(node, visit);
    }
    visit(ast);
    return issues;
}

// Keep quoted/multiline attributes and false branches covered. ARIA is not boolean HTML.
assert.equal(inspect(`E('button', {'disabled': false});`, 'bad.js').length, 1);
assert.equal(inspect(`E('input', {checked:\n !!enabled});`, 'bad.js').length, 1);
assert.equal(inspect(`E('option', {selected: flag ? true : false});`, 'bad.js').length, 1);
assert.equal(inspect(`E('div', {hidden: 'false'});`, 'bad.js').length, 1);
assert.equal(inspect(`dom.create('button', {disabled: !writable});`, 'bad.js').length, 1);
assert.equal(inspect(`node.setAttribute('disabled', false);`, 'bad.js').length, 1);
assert.deepEqual(inspect(`
    E('button', {disabled: !writable || null});
    E('input', {checked: enabled ? '' : null});
    E('audio', {controls: true, muted: ''});
    node.disabled = false;
    node.setAttribute('aria-disabled', 'false');
    new ui.Dropdown(value, options, {multiple: false});
    const payload = {disabled: false};
    sipui.button('Play', action, false, {disabled: seconds <= 0});
`, 'good.js'), []);

const skip = new Set(['node_modules', 'vendor', 'out', 'work', 'release', 'releases', 'dumps', 'tests', '.git']);
let count = 0;
const issues = [];
function scan(dir) {
    if (!fs.existsSync(dir)) return;
    for (const entry of fs.readdirSync(dir, {withFileTypes:true})) {
        const file = path.join(dir, entry.name);
        if (entry.isDirectory() && !skip.has(entry.name)) scan(file);
        if (entry.isFile() && entry.name.endsWith('.js') && !entry.name.startsWith('chart')) {
            count++;
            issues.push(...inspect(fs.readFileSync(file, 'utf8'), file));
        }
    }
}
const roots = process.argv.slice(2);
if (roots.length) {
    for (const root of roots) {
        for (const folder of ['openwrt', 'devices', 'installer/runtime']) scan(path.resolve(root, folder));
    }
} else scan(path.resolve(__dirname, '../runtime'));
if (issues.length) {
    console.error(issues.join('\n'));
    process.exitCode = 1;
} else console.log('LuCI boolean attributes: self-tests passed; ' + count + ' source files checked.');
