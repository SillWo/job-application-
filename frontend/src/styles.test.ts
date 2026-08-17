import cssSource from './styles.css?raw'

const css = cssSource.replace(/\s+/gu, '')

test('desktop shell reserves the left sidebar column and anchors it to the viewport left edge', () => {
  expect(css).toContain('.shell{display:grid;grid-template-columns:244pxminmax(0,1fr);min-height:100vh;}')
  expect(css).toContain('.sidebar{position:fixed;inset:0auto00;grid-column:1;width:244px;')
  expect(css).toContain('main{grid-column:2;min-width:0;}')
  expect(css).not.toMatch(/\.sidebar\{[^}]*right:/u)
  expect(css).not.toMatch(/\.sidebar\{[^}]*calc\(/u)
  expect(css).not.toMatch(/\.shell\{[^}]*max-width:/u)
})

test('mobile breakpoint restores top navigation flow', () => {
  expect(css).toContain('@media(max-width:900px){.shell{display:block;}')
  expect(css).toContain('.sidebar{position:static;inset:auto;')
  expect(css).toContain('main{grid-column:auto;}')
})
