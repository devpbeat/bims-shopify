export type Locale = 'es' | 'en'

export interface HelpSection {
  title: string
  what: string
  howAutoResolves: string
  manualActions: string
  ifNothingDoneTitle: string
  ifNothingDone: string
}

export interface Messages {
  common: {
    signIn: string
    signingIn: string
    haveAccessKey: string
    backToLogin: string
    continueLabel: string
    logout: string
    reportDate: string
    lastSync: string
    never: string
    loadingReport: string
    sessionExpired: string
    loadFailed: string
    applyFailed: string
    noReport: string
    loadingActivity: string
    loadMore: string
    loading: string
    noActivity: string
    pendingDecision: string
    optionalNote: string
    ignore: string
    approve: string
    keep: string
    delete: string
    queued: string
    queuedIgnore: string
    queuedApprove: string
    queuedKeep: string
    queuedDelete: string
    noPendingChanges: string
    pendingChange: string
    pendingChanges: string
    applyChanges: string
    applying: string
    close: string
    viewGuide: string
    dismiss: string
    languageToggleLabel: string
  }
  login: {
    title: string
    subtitle: (slug: string) => string
    emailPlaceholder: string
    passwordPlaceholder: string
    tooManyAttempts: string
    invalidCredentials: string
    loginFailedGeneric: string
    accessKeyPrompt: (slug: string) => string
    accessKeyPlaceholder: string
  }
  header: {
    subtitle: string
  }
  tabs: {
    duplicates: string
    unresolved: string
    mismatches: string
    activity: string
  }
  banner: {
    text: string
  }
  status: {
    applied: string
    recorded: string
    failed: string
    already_resolved: string
  }
  duplicates: {
    empty: string
    targetSku: string
    colKeep: string
    colProduct: string
    colOldSku: string
    colBimsName: string
    colStatus: string
  }
  unresolved: {
    empty: string
    colProduct: string
    colSku: string
    colNote: string
    colStatus: string
  }
  mismatches: {
    empty: string
    colProduct: string
    colBimsName: string
    colOldSku: string
    colNewSku: string
    colStatus: string
  }
  activity: {
    colTime: string
    colActor: string
    colAction: string
    colDetail: string
  }
  help: {
    modalTitleSuffix: string
    duplicates: HelpSection
    unresolved: HelpSection
    mismatches: HelpSection
  }
  operator: {
    entryLabel: string
    gateTitle: string
    gatePrompt: (slug: string) => string
    tokenPlaceholder: string
    enter: string
    invalidToken: string
    exit: string
    tabLabel: string
    jobHistoryTitle: string
    noJobs: string
    resultLabel: string
    runButton: string
    running: string
    jobAlreadyRunning: string
    confirmTitle: string
    confirmCancel: string
    confirmProceed: string
    advancedDisclosure: string
    dryRun: string
    apply: string
    onlyWithStock: string
    publish: string
    limit: string
    force: string
    forceWarning: string
    autoResolve: string
    typeDomainToConfirm: (domain: string) => string
    domainMismatch: string
    commands: {
      status: { title: string; description: string }
      import: { title: string; description: string; confirmApply: string }
      dedupe: { title: string; description: string; confirmApply: string }
      wipe: { title: string; description: string; confirmApply: string }
      rekey: { title: string; description: string; confirmApply: string }
      fixTracking: { title: string; description: string; confirmApply: string }
    }
    jobStatus: {
      queued: string
      running: string
      succeeded: string
      failed: string
      interrupted: string
    }
    columns: {
      command: string
      status: string
      time: string
    }
  }
}
