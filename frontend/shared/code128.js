// Code 128 barcodes as SVG, for product labels. Code set C (two digits per
// symbol) when the whole value is an even number of digits, else code set B
// (printable ASCII). Every scanner reads Code 128.

// Bar/space widths for symbol values 0-106 (106 = stop, 7 elements).
export const PATTERNS = [
  "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212", "221213",
  "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221", "223211", "221132",
  "221231", "213212", "223112", "312131", "311222", "321122", "321221", "312212", "322112", "322211",
  "212123", "212321", "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
  "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121", "313121", "211331",
  "231131", "213113", "213311", "213131", "311123", "311321", "331121", "312113", "312311", "332111",
  "314111", "221411", "431111", "111224", "111422", "121124", "121421", "141122", "141221", "112214",
  "112412", "122114", "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
  "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
  "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311", "113141",
  "114131", "311141", "411131", "211412", "211214", "211232", "2331112",
];

const START_B = 104;
const START_C = 105;
const STOP = 106;

/** Symbol values (start, data, checksum, stop) for `text`. */
export function encode(text) {
  const value = String(text);
  if (!value) throw new Error("empty barcode");
  let codes;
  if (/^\d+$/.test(value) && value.length % 2 === 0) {
    codes = [START_C];
    for (let i = 0; i < value.length; i += 2) codes.push(Number(value.slice(i, i + 2)));
  } else {
    codes = [START_B];
    for (const ch of value) {
      const c = ch.charCodeAt(0);
      if (c < 32 || c > 127) throw new Error(`can't encode ${JSON.stringify(ch)} in Code 128`);
      codes.push(c - 32);
    }
  }
  let sum = codes[0];
  for (let i = 1; i < codes.length; i++) sum += codes[i] * i;
  codes.push(sum % 103, STOP);
  return codes;
}

/** Module widths, alternating bar/space, starting with a bar. */
export function modules(text) {
  return encode(text).flatMap((c) => PATTERNS[c].split("").map(Number));
}

/** An SVG string: bars only (the caller prints the human-readable text). */
export function svg(text, { height = 50, module = 2, quiet = 10 } = {}) {
  const widths = modules(text);
  const total = widths.reduce((a, b) => a + b, 0) + quiet * 2;
  let x = quiet;
  let rects = "";
  widths.forEach((w, i) => {
    if (i % 2 === 0) rects += `<rect x="${x}" y="0" width="${w}" height="${height}"/>`;
    x += w;
  });
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${total} ${height}" width="${total * module}" height="${height}" preserveAspectRatio="none" shape-rendering="crispEdges">${rects}</svg>`;
}
