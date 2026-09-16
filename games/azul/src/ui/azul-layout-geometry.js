(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.BGLabAzulLayoutGeometry = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const BOARD = Object.freeze({
    width:680,
    height:460,
    patternRightCenters:Object.freeze([290.5, 290.5, 290.5, 290.5, 290.5]),
    patternRowCenters:Object.freeze([46.5, 110.5, 174.5, 238.5, 302.5]),
    wallFirstCenter:Object.freeze({x:376, y:50}),
    floorFirstCenter:Object.freeze({x:37.5, y:407.5}),
  });
  const FACTORY_STAGE = Object.freeze({width:500, height:500});

  function factoryPositions(count) {
    const {size, radius} = count <= 5 ? {size:140, radius:160}
      : count <= 7 ? {size:112, radius:180} : {size:90, radius:200};
    return Array.from({length:count}, (_, index) => {
      const angle = index / count * Math.PI * 2 - Math.PI / 2;
      return {
        x:FACTORY_STAGE.width / 2 + radius * Math.cos(angle) - size / 2,
        y:FACTORY_STAGE.height / 2 + radius * Math.sin(angle) - size / 2,
        scale:size / 140,
      };
    });
  }

  function viewportFor(width, stage) {
    const scale = Math.min(1, Math.max(0, Number(width) || 0) / stage.width);
    return {scale, width:stage.width * scale, height:stage.height * scale};
  }

  return Object.freeze({BOARD, FACTORY_STAGE, factoryPositions, viewportFor});
});
