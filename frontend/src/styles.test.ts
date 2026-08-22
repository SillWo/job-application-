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
