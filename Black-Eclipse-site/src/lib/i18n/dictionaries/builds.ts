// The Builds surface's project copy. Split out of `en.ts` (which is at its size
// ratchet) the same way `evolution.ts` / `capability-card.ts` are.

export const builds = {
  // Which folder new builds belong to.
  'code.project.label': 'This project',
  'code.project.switch': 'Change',
  'code.project.defaultName': 'Eclipse repo',
  'code.project.defaultOption': 'Use the built-in repo',
  'code.project.openFolder': 'Open folder…',
  'code.project.useFolder': 'Build here',
  'code.project.none': 'No folders approved yet.',
  'code.project.failed': 'I could not read your approved folders just now.',
  'code.project.remove': 'Remove from list',

  // The pre-build project brief (P5).
  'code.project.preparing': 'Preparing…',
  'code.project.prepared': 'Prepared',
  'code.project.prepare': 'Prepare project',
  'code.project.dirty': '{count} changed',
  'code.project.rulesOn': 'Project rules loaded',
  'code.project.noRun': 'No run command found',
  'code.project.checksLabel': 'Checks',
  'code.project.checks.detected': 'detected',
  'code.project.checks.project': 'project',
  'code.project.checks.defaults': 'Eclipse defaults',
  'code.project.checks.none': 'none',

  // Run and preview (P2).
  'code.preview.title': 'See it running',
  'code.preview.hint': 'Runs the project as it is on disk. A change you have not landed yet is not included.',
  'code.preview.run': 'Run the project',
  'code.preview.willRun': 'Runs {command}',
  'code.preview.starting': 'Starting…',
  'code.preview.checkAgain': 'Check again',
  'code.preview.open': 'Open',
  'code.preview.stop': 'Stop',
  'code.preview.stopped': 'Stopped.',
  'code.preview.notReady': 'Running, but nothing is listening yet.',
  'code.preview.logs': 'Show logs',
  'code.preview.hideLogs': 'Hide logs',
  'code.preview.noLogs': 'No output yet.',
  'code.preview.refused': 'I could not find how to run this project. Add .black-eclipse/preview.json with a command.',
  'code.preview.failed': 'I could not start the project just now.',
  'code.preview.unavailable': 'The Eclipse runtime is not running, so the project cannot be started.',

  // Which builds the scorecard counts.
  'code.scorecard.scope.project': 'This project',
  'code.scorecard.scope.all': 'All builds',

  // W7 file checkpoints + /rollback.
  'code.checkpoints.title': 'File snapshots',
  'code.checkpoints.empty': 'No snapshots yet. Eclipse takes one before the first file change.',
  'code.checkpoints.refresh': 'Refresh',
  'code.checkpoints.restore': 'Restore',
  'code.checkpoints.undoTurn': 'Undo last turn',
  'code.checkpoints.undoTurnConfirm': 'Undo every tracked file change made in the last turn?',
  'code.checkpoints.confirm': 'Restore every tracked change from this snapshot?',
  'code.checkpoints.restored': 'Restored',
  'code.checkpoints.undoPoint': 'Undo point',
  'code.checkpoints.restoreFailed': 'That snapshot could not be restored.',
  'code.checkpoints.loadFailed': 'Could not load file snapshots.',
  'code.checkpoints.noDiff': '(no differences)',
  'code.chat.attachImage': 'Attach image',
  'code.chat.removeImage': 'Remove image',
  'code.chat.imageTooLarge': 'That image is too large (2 MB max).',
  'code.chat.imageFailed': 'That image could not be read.',
} as const;
