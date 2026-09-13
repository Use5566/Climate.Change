// Offsets are Unicode code points, matching Python string slicing.
export function mergeRanges(ranges) {
  const result = [];
  for (const range of ranges.map(x => ({...x})).sort((a,b) => a.p-b.p || a.start-b.start)) {
    const last = result.at(-1);
    if (last && last.p === range.p && range.start <= last.end) last.end = Math.max(last.end, range.end);
    else result.push(range);
  }
  return result;
}
export function isCovered(highlights, selection) {
  return selection.length > 0 && selection.every(s => highlights.some(h => h.p === s.p && h.start <= s.start && h.end >= s.end));
}
export function toggleRanges(highlights, selection) {
  if (!isCovered(highlights, selection)) return mergeRanges([...highlights, ...selection]);
  let result = highlights.map(x => ({...x}));
  for (const s of selection) {
    result = result.flatMap(h => {
      if (h.p !== s.p || s.end <= h.start || s.start >= h.end) return [h];
      const pieces = [];
      if (h.start < s.start) pieces.push({...h, end:s.start});
      if (h.end > s.end) pieces.push({...h, start:s.end});
      return pieces;
    });
  }
  return result;
}
export function excerpt(text, start, end) { return Array.from(text).slice(start,end).join(''); }

export function highlightLength(text) { return Array.from(text.replace(/[\p{P}\s]/gu, '')).length; }
export function highlightsWithinLimit(ranges, paragraphs) {
  return mergeRanges(ranges).every(h => highlightLength(excerpt(paragraphs[h.p].text, h.start, h.end)) <= 30);
}

export function installInferenceCopyGuard(isActive) {
  for (const type of ['copy', 'cut', 'dragstart', 'contextmenu']) {
    document.addEventListener(type, event => {
      if (isActive()) event.preventDefault();
    }, true);
  }
  document.addEventListener('keydown', event => {
    if (isActive() && (event.ctrlKey || event.metaKey) && ['c', 'x'].includes(event.key.toLowerCase())) {
      event.preventDefault();
    }
  }, true);
}
