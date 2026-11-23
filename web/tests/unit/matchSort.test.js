import { describe, it, expect, beforeEach } from 'vitest';

// browse/matchSort.js — 現在フォルダ直下の子要素をクエリで relevance ソート。
// /api/v1/search は使わない — window.tree のノード (name/metadata) だけで採点。
const BMS = globalThis.BrowseMatchSort;

function node(name, meta) {
  const n = { name, _fullPath: 'lib/' + name, type: 'folder' };
  if (meta) n.metadata = meta;
  return n;
}

function names(list) { return list.map((n) => n.name); }

beforeEach(() => {
  BMS.clear();
  window.currentBrowsePath = 'lib';
  document.body.innerHTML = '<input id="search-input" value="">';
});

describe('apply — scope & activation', () => {
  it('returns the same array when no query is set', () => {
    const kids = [node('b'), node('a')];
    expect(BMS.apply(kids, 'lib')).toBe(kids);
  });

  it('returns the same array for a whitespace-only query', () => {
    BMS.setQuery('   ');
    const kids = [node('b'), node('a')];
    expect(BMS.apply(kids, 'lib')).toBe(kids);
  });

  it('does not sort a different level (underlay / other folder)', () => {
    BMS.setQuery('vol');           // captured level = 'lib'
    const kids = [node('beta'), node('vol1'), node('alpha')];
    expect(names(BMS.apply(kids, 'other'))).toEqual(['beta', 'vol1', 'alpha']);
  });

  it('sorts only the level the query was typed at', () => {
    window.currentBrowsePath = 'lib/series';
    BMS.setQuery('vol');           // level = 'lib/series'
    const kids = [node('beta'), node('vol1')];
    expect(names(BMS.apply(kids, 'lib/series'))).toEqual(['vol1', 'beta']);
    expect(names(BMS.apply(kids, 'lib'))).toEqual(['beta', 'vol1']);
  });

  it('root level (null) matches a null path argument', () => {
    window.currentBrowsePath = null;
    BMS.setQuery('manga');
    const kids = [node('other'), node('my_manga')];
    expect(names(BMS.apply(kids, null))).toEqual(['my_manga', 'other']);
    expect(names(BMS.apply(kids, 'lib'))).toEqual(['other', 'my_manga']);
  });
});

describe('apply — ordering', () => {
  it('moves name matches to the front and keeps every child', () => {
    BMS.setQuery('vol');
    const kids = [node('alpha'), node('vol2'), node('beta'), node('vol1')];
    // vol1/vol2 tie on score (prefix bonus) → stable: input order vol2 before vol1
    expect(names(BMS.apply(kids, 'lib'))).toEqual(['vol2', 'vol1', 'alpha', 'beta']);
  });

  it('prefix match outranks mid-string match on the same field', () => {
    BMS.setQuery('vol');
    const kids = [node('Evolution'), node('vol1')];
    expect(names(BMS.apply(kids, 'lib'))).toEqual(['vol1', 'Evolution']);
  });

  it('exact name match outranks prefix match', () => {
    BMS.setQuery('vol1');
    const kids = [node('vol10'), node('vol1')];
    // 'vol1' exact (+16) > 'vol10' prefix (+13)
    expect(names(BMS.apply(kids, 'lib'))).toEqual(['vol1', 'vol10']);
  });

  it('zero-score children keep their original relative order at the end', () => {
    BMS.setQuery('zzz');
    const kids = [node('c'), node('a'), node('b')];
    expect(names(BMS.apply(kids, 'lib'))).toEqual(['c', 'a', 'b']);
  });

  it('does not mutate the input array', () => {
    BMS.setQuery('vol');
    const kids = [node('beta'), node('vol1')];
    const snapshot = names(kids);
    BMS.apply(kids, 'lib');
    expect(names(kids)).toEqual(snapshot);
  });
});

describe('apply — metadata matching', () => {
  it('matches title_jp / title_en', () => {
    BMS.setQuery('テスト');
    const kids = [
      node('plain'),
      node('work', { title_jp: 'テスト作品', title_en: '', artist: [], tags: [] }),
    ];
    expect(names(BMS.apply(kids, 'lib'))).toEqual(['work', 'plain']);
  });

  it('matches artist names', () => {
    BMS.setQuery('author');
    const kids = [
      node('plain'),
      node('work', { title_jp: '', title_en: '', artist: ['Some Author'], tags: [] }),
    ];
    expect(names(BMS.apply(kids, 'lib'))).toEqual(['work', 'plain']);
  });

  it('matches tags', () => {
    BMS.setQuery('seinen');
    const kids = [
      node('plain'),
      node('work', { title_jp: '', title_en: '', artist: [], tags: ['seinen', 'action'] }),
    ];
    expect(names(BMS.apply(kids, 'lib'))).toEqual(['work', 'plain']);
  });

  it('multi-field hits outrank single-field hits of the same word', () => {
    BMS.setQuery('vol');
    const kids = [
      node('vol_only'),                                     // name only: 10+3=13
      node('other', { title_jp: 'vol', title_en: '', artist: ['vol'], tags: [] }), // title+artist: 18+2=20
    ];
    expect(names(BMS.apply(kids, 'lib'))).toEqual(['other', 'vol_only']);
  });

  it('multi-word queries score each word independently', () => {
    BMS.setQuery('vol 作者');
    const kids = [
      node('vol1'),                                                        // 'vol' name hit only
      node('work', { title_jp: '', title_en: '', artist: ['作者'], tags: [] }), // '作者' artist hit only
      node('vol_work', { title_jp: '', title_en: '', artist: ['作者'], tags: [] }), // both
    ];
    // vol_work hits name('vol')+artist('作者') → highest
    expect(names(BMS.apply(kids, 'lib'))[0]).toBe('vol_work');
  });

  it('nodes without metadata still match on name', () => {
    BMS.setQuery('manga');
    const kids = [node('book'), node('manga_01')];
    expect(names(BMS.apply(kids, 'lib'))).toEqual(['manga_01', 'book']);
  });
});

describe('setQuery / syncLevel / clear', () => {
  it('setQuery captures the current browse level', () => {
    window.currentBrowsePath = 'lib/a';
    BMS.setQuery('x');
    expect(BMS._state.level).toBe('lib/a');
    expect(BMS._state.text).toBe('x');
    expect(BMS.isActive()).toBe(true);
  });

  it('syncLevel keeps the query on the same level', () => {
    BMS.setQuery('vol');
    BMS.syncLevel('lib');
    expect(BMS.isActive()).toBe(true);
  });

  it('syncLevel clears the query when the level changes', () => {
    BMS.setQuery('vol');
    BMS.syncLevel('lib/other');
    expect(BMS.isActive()).toBe(false);
    expect(BMS._state.text).toBe('');
  });

  it('syncLevel does nothing while the query is empty', () => {
    BMS.syncLevel('anywhere');
    expect(BMS.isActive()).toBe(false);
  });

  it('clear() empties the input element too', () => {
    const si = document.getElementById('search-input');
    si.value = 'vol';
    BMS.setQuery('vol');
    BMS.clear();
    expect(si.value).toBe('');
    expect(BMS.isActive()).toBe(false);
  });
});
