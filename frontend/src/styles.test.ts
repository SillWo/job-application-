import cssSource from './styles.css?raw'

const css = cssSource.replace(/\s+/gu, '')

test('desktop shell is full width with centered top navigation and main content', () => {
  expect(css).toContain('.shell{min-height:100vh;width:100%;}')
  expect(css).toContain('.topbar{position:sticky;top:0;width:100%;')
  expect(css).toContain('.topbar-inner{width:100%;max-width:1280px;')
  expect(css).toContain('.page{width:100%;max-width:1280px;margin:auto;')
  expect(css).toContain('.shell,main,.stats,.grid2,.cards,.panel,.statsarticle{min-width:0;max-width:100%;}')
  expect(css).not.toContain('.shell,main,.page,.stats,.grid2,.cards,.panel,.statsarticle{')
  expect(css).toContain('main{width:100%;min-width:0;}')
  expect(css).toContain('.topbar-actions>a,.button-link{display:inline-flex;')
  expect(css).toContain('.topbar-nav{display:flex;align-items:center;gap:4px;min-width:0;overflow-x:auto;')
  expect(css).not.toContain('.privacy-summary')
  expect(css).not.toContain('.model-status')
  expect(css).not.toContain('.dot')
  expect(css).not.toContain('header>div{')
  expect(css).not.toContain('headera,')
  expect(css).not.toContain('header{')
  expect(css).not.toContain('grid-template-columns:244pxminmax(0,1fr)')
  expect(css).not.toContain('.sidebar{')
})

test('session PDF report uses a compact branded action link', () => {
  expect(css).toContain('.session-actions{justify-content:flex-end;align-items:center;}')
  expect(css).toContain('.session-report-button{min-height:40px;gap:8px;')
  expect(css).toContain('.session-report-icon{width:18px;height:18px;')
})

test('mobile breakpoint places horizontal top navigation on a second row', () => {
  expect(css).toContain('@media(max-width:900px){.topbar-inner{grid-template-columns:1frauto;grid-template-rows:autoauto;')
  expect(css).toContain('.topbar-nav{grid-column:1/-1;grid-row:2;overflow-x:auto;')
})

test('form width tokens and utilities define bounded, mobile-safe rails', () => {
  expect(css).toContain('--form-content-max:880px;')
  expect(css).toContain('--field-compact:320px;')
  expect(css).toContain('--field-medium:480px;')
  expect(css).toContain('--field-wide:720px;')
  expect(css).toContain('--field-prose:760px;')
  expect(css).toContain('.form-rail,.field-compact,.field-medium,.field-wide,.field-prose{width:100%;}')
  expect(css).toContain('.form-rail,.form-row{max-width:var(--form-content-max);}')
  expect(css).toContain('.form-row{width:100%;justify-content:start;}')
  expect(css).toContain('@media(max-width:600px){.topbar{min-height:60px;}')
  expect(css).toContain('.field-compact,.field-medium,.field-wide,.field-prose{max-width:none;}')
})

test('all page content shares the centered influence rail without shrinking panel content twice', () => {
  const rail = '.page>*{width:100%;max-width:calc(var(--form-content-max)+42px);margin-inline:auto;}'
  const safeguard = '.shell,main,.stats,.grid2,.cards,.panel,.statsarticle{min-width:0;max-width:100%;}'
  expect(css).toContain(rail)
  expect(css.indexOf(rail)).toBeGreaterThan(css.indexOf(safeguard))
  expect(css).not.toContain('.session-page>')
})

test('resume editor fields and repeated experience cards have explicit boundaries', () => {
  expect(css).toContain('.resume-editor.form-contentinput:not([type="checkbox"]),.resume-editor.form-contentselect,.resume-editor.form-contenttextarea{border-color:var(--line);background:var(--paper);}')
  expect(css).toContain('.experience-card{border:1pxsolidvar(--line);background:var(--paper);')
  expect(css).toContain('.experience-card>.repeat-row{padding-bottom:10px;border-bottom:1pxsolidvar(--line);}')
})

test('multi-select uses aligned bordered rows and a full-width confirmation action', () => {
  expect(css).toContain('.multi-select-trigger{display:flex;align-items:center;justify-content:space-between;')
  expect(css).toContain('.multi-select-chevron.is-open{transform:rotate(180deg);}')
  expect(css).toContain('.multi-select-popover{display:grid;gap:10px;padding:12px;border:1pxsolidvar(--line);')
  expect(css).toContain('.formlabel.multi-select-option{display:flex;align-items:center;gap:12px;')
  expect(css).toContain('.formlabel.multi-select-option.is-selected{border-color:var(--ink);background:var(--surface);}')
  expect(css).toContain('.multi-select-confirm{width:100%;}')
})

test('all checkboxes share the resume control size and centered wrapper alignment', () => {
  expect(css).toContain('input[type="checkbox"]{box-sizing:border-box;flex:0018px;width:18px;height:18px;margin:0;padding:0;accent-color:var(--ink);cursor:pointer;}')
  expect(css).toContain('input[type="checkbox"]:disabled{cursor:not-allowed;}')
  expect(css).toContain('.checkline,.formlabel.checkline{display:flex;align-items:center;gap:8px;')
  expect(css).toContain('.selection-control{display:flex;align-items:center;gap:8px;')
})

test('profile layout keeps explicit equal and removable rails', () => {
  expect(css).toContain('.profile-pair-row,.profile-pair-remove-row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));')
  expect(css).toContain('.profile-remove-row{display:grid;grid-template-columns:minmax(0,1fr)auto;')
  expect(css).toContain('.profile-pair-remove-row{grid-template-columns:repeat(2,minmax(0,1fr))auto;}')
  expect(css).toContain('.profile-pair-row,.profile-pair-remove-row{grid-template-columns:minmax(0,1fr);}')
  expect(css).toContain('.profile-pair-remove-row>:nth-child(1),.profile-pair-remove-row>:nth-child(2){grid-column:1;}')
  expect(css).toContain('.profile-pair-remove-row>.icon-button{grid-column:2;grid-row:1/3;')
})

test('notification items remain readable and opaque despite global button styles', () => {
  expect(css).toContain('.notification-item{display:grid;gap:4px;width:100%;border:0;border-bottom:1pxsolidvar(--line);background:transparent;color:var(--ink);opacity:1;')
  expect(css).toContain('.notification-item:hover,.notification-item:focus-visible{opacity:1;}')
  expect(css).toContain('.notification-itemstrong,.notification-itemspan{color:var(--ink);font-size:13px;line-height:1.35;}')
  expect(css).toContain('.notification-item:hover,.notification-item.unread{background:var(--surface);}')
})

test('influence tooltips use a layered DOM tooltip with an italic footnote', () => {
  expect(css).toContain('.influence-control:focus-within,.influence-control:hover{z-index:3;}')
  expect(css).toContain('.tooltip{position:absolute;z-index:4;')
  expect(css).toContain('width:min(360px,calc(100vw-48px));')
  expect(css).toContain('.tooltip-wrap:hover.tooltip,.tooltip-wrap:focus-within.tooltip{opacity:1;transform:translateY(0);}')
  expect(css).toContain('.tooltipem{display:block;margin-top:7px;font-style:italic;}')
  expect(css).toContain('.question-button:hover{opacity:1;}')
  expect(css).not.toContain('.question-button::after')
})

test('influence labels share the range axis at each slider stop', () => {
  expect(css).toContain('.influence-axis{position:relative;padding-inline:clamp(42px,5vw,52px);}')
  expect(css).toContain('.forminput.influence-range{display:block;width:100%;height:18px;margin:0;padding:0;border:0;')
  expect(css).toContain('appearance:none;')
  expect(css).toContain('.influence-levels{position:relative;height:18px;margin:7px9px0;')
  expect(css).not.toContain('inset:25px0auto')
  expect(css).toContain('left:var(--level-position);transform:translateX(-50%);')
  expect(css).not.toContain('grid-template-columns:repeat(3,1fr)')
})
