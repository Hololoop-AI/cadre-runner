// The fleet model: statusd.py's home-page logic in TypeScript, pure (no
// React, no network, no DOM). Each function is named after the Python one it
// copied, so where a rule came from can still be found. This file is the
// module's only public entry.

export type {
  Backend, Backends, ClosedPage, Kanban, KanbanCard, CostBucket, CostSummary, Dirs, Earlier, Ending, Fleet, InstalledEntry, Library, RunRow, Runs,
  PageOwner, PagePayload, ProjectContext, ProjectPayload, TreePayload, TurnRecord, ItemRecord, LibraryEntry, LinkRecord, NewWorkInfo, PageInfo, Pageless, Project,
  RunningCard, SessionStatus, Surface, TreeNode, ViewInfo, Nav, NavLink, CadreSettings, CadreAgent, Blackboard, BlackboardEvent, Deck, DeckCard, DeckTopic, StudySession, CadreRecord, CadreRecords,
  Notification, Notifications,
} from './payload';
export { bellStep, chimeId, isUnread, unreadNotifications } from './notifications';
export { cadreCards, itemCadre, needsYou, type CadreCard } from './cadres';
export { cmp, escapedCut, hasKey, homePath, num, own, pyFixed, relTime, s, taskLabel, treeHref, usd, viewHref } from './format';
export { collapse, findPages, keyOf, statusOf, type Hit } from './conversations';
export { isoEpoch } from './format';
export {
  answered, catchUpText, DONE, endingPhrase, HIDDEN, LABELS, pagelessPhrase, pageState, SORTS, WORKING,
  type Item, type State,
} from './items';
export { buildItemGraph, ROW, x, type Dot, type Graph, type GraphRow } from './graph';
export { effectiveGroup, fleetGroups, groupBadges, groupTag, HOLO, HOME_GROUP, inGroup, itemGroups, OTHER, type Group } from './groups';
export { activeCards, byColumn, cardItemHref, cardRuns, cardTally, columnLabel, DRIVER, kanbanMove, pruneKids, runPages, runsOnCards, type CardRun, type CardTally } from './kanban';
export { liveProjects } from './projects';
export { cardFacts, LOOSE_FRESH_S, workRows, type CardFacts, type WorkRow, type WorkRows, type WorkView } from './work';
export { ancestors, clean, descendants, kidsOf, parentsOf } from './tree';
export type { Tree } from './tree';
export {
  CHIPS, effectiveView, layout, loadSel, saveSel, serverView, tagChips, type LayoutInput, type Placed, type Sel, type ServerView,
} from './view';
export { buildHome, limitChoices, staleCode, type Home, type ItemContext } from './home';
export { findEntries, folderPicks, projectHint } from './picker';
export { plural, pyRound, RUN_SORTS, RUN_VIEWS, runsHref, runsSel, runsView, treeRoots, type RunsSel } from './screens';
export { externalProjects, groupRows, liveBadge, projectSection, runLibrary, startingWhat, touchedAt, type SectionPart } from './sections';
