// The fleet model: statusd.py's home-page logic in TypeScript, pure (no
// React, no network, no DOM). Each function is named after the Python one it
// copies, so a change on one side can be found on the other. This file is
// the module's only public entry; the parity harness (web/parity/) is what
// proves the copies still agree.

export type {
  Backend, Backends, ClosedPage, CostBucket, CostSummary, Dirs, Earlier, Ending, Fleet, InstalledEntry, Library, RunRow, Runs,
  PageOwner, PagePayload, ProjectContext, ProjectPayload, TreePayload, TurnRecord, ItemRecord, LibraryEntry, LinkRecord, NewWorkInfo, PageInfo, Pageless, Project,
  RunningCard, SessionStatus, Surface, TreeNode,
} from './payload';
export { cmp, escapedCut, hasKey, homePath, num, own, pyFixed, relTime, s, taskLabel, treeHref, usd, viewHref } from './format';
export { collapse, findPages, keyOf, statusOf, type Hit } from './conversations';
export { isoEpoch } from './format';
export {
  answered, catchUpText, DONE, endingPhrase, HIDDEN, LABELS, pagelessPhrase, pageState, SORTS, WORKING,
  type Item, type State,
} from './items';
export { buildItemGraph, ROW, x, type Dot, type Graph, type GraphRow } from './graph';
export { effectiveGroup, fleetGroups, groupBadges, groupTag, HOME_GROUP, inGroup, itemGroups, OTHER, type Group } from './groups';
export { liveProjects } from './projects';
export { ancestors, clean, descendants, kidsOf, parentsOf } from './tree';
export type { Tree } from './tree';
export {
  CHIPS, effectiveView, layout, loadSel, saveSel, serverView, tagChips, type LayoutInput, type Placed, type Sel, type ServerView,
} from './view';
export { buildHome, limitChoices, staleCode, type Home, type ItemContext } from './home';
export { findEntries, folderPicks, projectHint } from './picker';
export { plural, pyRound, RUN_SORTS, RUN_VIEWS, runsHref, runsSel, runsView, treeRoots, type RunsSel } from './screens';
export { externalProjects, groupRows, liveBadge, projectSection, runLibrary, startingWhat, touchedAt, type SectionPart } from './sections';
