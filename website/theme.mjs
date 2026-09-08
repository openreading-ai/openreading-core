/** Shared diagram tokens keep GitHub, exported vectors, and the field guide visually aligned. */
export const theme = {
  theme: 'base',
  fontFamily: 'Arial',
  deterministicIds: true,
  deterministicIDSeed: 'openreading',
  htmlLabels: false,
  themeVariables: {
    fontFamily: 'Arial', fontSize: '17px',
    lineColor: '#8194ad', textColor: '#183451', primaryTextColor: '#183451',
    primaryColor: '#edf3fc', primaryBorderColor: '#9db4d0',
    edgeLabelBackground: '#ffffff', clusterBkg: '#f5f8fc', clusterBorder: '#d7e1ee',
    titleColor: '#183451', actorBkg: '#edf3fc', actorBorder: '#9db4d0',
    actorTextColor: '#183451', actorLineColor: '#9db4d0', signalColor: '#527095',
    signalTextColor: '#183451', labelBoxBkgColor: '#fff4de', labelBoxBorderColor: '#c6953a',
    labelTextColor: '#70501b', loopTextColor: '#527095', noteBkgColor: '#edf3fc',
    noteBorderColor: '#9db4d0', noteTextColor: '#183451', sequenceNumberColor: '#ffffff',
    activationBkgColor: '#e7f3ee', activationBorderColor: '#679780',
  },
  flowchart: { curve: 'monotoneY', nodeSpacing: 32, rankSpacing: 48, padding: 18, useMaxWidth: true },
  sequence: { useMaxWidth: true, actorMargin: 65, messageMargin: 38, mirrorActors: false },
};
export const classes = {
  src: ['#f5f8fc', '#a7b9d0', '#29445f'],
  work: ['#edf3fc', '#9db4d0', '#183451'],
  gate: ['#fff4de', '#c6953a', '#70501b'],
  good: ['#e7f3ee', '#679780', '#245740'],
  bad: ['#fbeeee', '#c78686', '#803d3d'],
  store: ['#e7f3ee', '#679780', '#245740'],
  out: ['#edf3fc', '#9db4d0', '#183451'],
  hero: ['#164bc5', '#164bc5', '#ffffff'],
};
export function styleDiagram(source) {
  const body = source.replace(/^%%\{init:.*\}%%\n/gm, '')
    .replace(/^\s*classDef .*\n/gm, '').replace(/^\s*linkStyle default.*\n?/gm, '').trim();
  const definitions = body.startsWith('flowchart') ? '\n' + Object.entries(classes)
    .map(([key, [fill, stroke, color]]) => `  classDef ${key} fill:${fill},stroke:${stroke},stroke-width:1px,color:${color};`)
    .join('\n') + '\n  linkStyle default stroke-width:1.4px;' : '';
  return `%%{init: ${JSON.stringify(theme)}}%%\n${body}${definitions}\n`;
}
