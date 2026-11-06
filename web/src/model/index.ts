// The fleet model: statusd.py's home-page logic in TypeScript, pure (no
// React, no network, no DOM). Each function is named after the Python one it
// copies, so a change on one side can be found on the other. This file is
// the module's only public entry; the parity harness (web/parity/) is what
// proves the copies still agree.

export type {
  Backend, Backends, Dirs, Earlier, Ending, Fleet, ItemRecord, LibraryEntry, LinkRecord, NewWorkInfo, PageInfo, Pageless, Project,
  RunningCard, SessionStatus, Surface, TreeNode,
} from './payload';
export { cmp, hasKey, homePath, num, own, pyFixed, relTime, s, taskLabel, treeHref, usd, viewHref } from './format';
export { findPages, keyOf, statusOf, type Hit } from './conversations';
export {
  answered, catchUpText, DONE, endingPhrase, HIDDEN, LABELS, pagelessPhrase, pageState, SORTS, WORKING,
  type Item, type State,
} from './items';
export { buildItemGraph, ROW, x, type Dot, type Graph, type GraphRow } from './graph';
export { liveProjects } from './projects';
export type { Tree } from './tree';
export {
  CHIPS, effectiveView, layout, loadSel, saveSel, serverView, tagChips, type LayoutInput, type Placed, type Sel, type ServerView,
} from './view';
export { buildHome, limitChoices, staleCode, type Home, type ItemContext } from './home';
export { findEntries, folderPicks, projectHint } from './picker';
