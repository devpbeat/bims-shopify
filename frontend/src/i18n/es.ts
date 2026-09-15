import type { Messages } from './types'

export const es: Messages = {
  common: {
    signIn: 'Iniciar sesión',
    signingIn: 'Iniciando sesión…',
    haveAccessKey: '¿Tenés una clave de acceso?',
    backToLogin: 'Volver a inicio de sesión con email y contraseña',
    continueLabel: 'Continuar',
    logout: 'Cerrar sesión',
    reportDate: 'Fecha del reporte',
    lastSync: 'Última sincronización',
    never: 'Nunca',
    loadingReport: 'Cargando reporte de reconciliación…',
    sessionExpired: 'La sesión expiró o las credenciales no son válidas. Iniciá sesión nuevamente.',
    loadFailed: 'No se pudo cargar el reporte de reconciliación.',
    applyFailed: 'No se pudieron aplicar las resoluciones.',
    noReport: 'No se encontró un reporte de reconciliación para esta tienda.',
    loadingActivity: 'Cargando actividad…',
    loadMore: 'Cargar más',
    loading: 'Cargando…',
    noActivity: 'Todavía no hay actividad registrada.',
    pendingDecision: 'Decisión pendiente',
    optionalNote: 'Nota opcional',
    ignore: 'Ignorar',
    approve: 'Aprobar',
    keep: 'Mantener',
    delete: 'Eliminar',
    queued: 'En cola',
    queuedIgnore: 'En cola: ignorar',
    queuedApprove: 'En cola: aprobar',
    queuedKeep: 'En cola: mantener',
    queuedDelete: 'En cola: eliminar',
    noPendingChanges: 'Sin cambios pendientes',
    pendingChange: 'cambio pendiente',
    pendingChanges: 'cambios pendientes',
    applyChanges: 'Aplicar cambios',
    applying: 'Aplicando…',
    close: 'Cerrar',
    viewGuide: 'Ver guía',
    dismiss: 'Entendido',
    languageToggleLabel: 'Idioma',
  },
  login: {
    title: 'Portal de la tienda',
    subtitle: (slug) => `Iniciá sesión en "${slug}" con tu email y contraseña.`,
    emailPlaceholder: 'Email',
    passwordPlaceholder: 'Contraseña',
    tooManyAttempts: 'Demasiados intentos de inicio de sesión. Esperá un minuto e intentá de nuevo.',
    invalidCredentials: 'Email o contraseña inválidos.',
    loginFailedGeneric: 'No se pudo iniciar sesión.',
    accessKeyPrompt: (slug) => `Ingresá el token de acceso para "${slug}" para continuar.`,
    accessKeyPlaceholder: 'Token de acceso del portal',
  },
  header: {
    subtitle: 'Auditoría de reconciliación de inventario',
  },
  tabs: {
    duplicates: 'Duplicados',
    unresolved: 'Sin resolver',
    mismatches: 'Diferencias de nombre',
    activity: 'Actividad',
  },
  banner: {
    text:
      'El inventario se reconcilia automáticamente con BIMS; esta pantalla es para revisar y auditar los cambios.',
  },
  status: {
    applied: 'aplicado',
    recorded: 'registrado',
    failed: 'falló',
    already_resolved: 'ya resuelto',
  },
  duplicates: {
    empty: 'No hay SKU duplicados en este reporte.',
    targetSku: 'SKU destino',
    colKeep: 'Mantener',
    colProduct: 'Producto',
    colOldSku: 'SKU anterior',
    colBimsName: 'Nombre en BIMS',
    colStatus: 'Estado',
  },
  unresolved: {
    empty: 'No hay variantes sin resolver en este reporte.',
    colProduct: 'Producto',
    colSku: 'SKU',
    colNote: 'Nota',
    colStatus: 'Estado',
  },
  mismatches: {
    empty: 'No hay diferencias de nombre en este reporte.',
    colProduct: 'Producto',
    colBimsName: 'Nombre en BIMS',
    colOldSku: 'SKU anterior',
    colNewSku: 'SKU nuevo',
    colStatus: 'Estado',
  },
  activity: {
    colTime: 'Fecha',
    colActor: 'Origen',
    colAction: 'Acción',
    colDetail: 'Detalle',
  },
  help: {
    modalTitleSuffix: '— Guía',
    duplicates: {
      title: 'Duplicados',
      what:
        'Ocurre cuando dos o más variantes de Shopify terminan apuntando al mismo SKU de BIMS. Esto suele pasar cuando un producto se recodificó y quedó una variante "vieja" y una "nueva" con el mismo código.',
      howAutoResolves:
        'La reconciliación automática elige una variante como sobreviviente (la que tiene el SKU correcto de BIMS) y elimina o desactiva la variante duplicada, para que el stock deje de contarse dos veces.',
      manualActions:
        'Si preferís resolverlo manualmente: el botón "Mantener" marca qué variante querés conservar; las demás del mismo grupo quedan en cola para eliminarse.',
      ifNothingDoneTitle: '¿Qué pasa si no se hace nada?',
      ifNothingDone:
        'El stock aparente se duplica: el mismo producto figura con el doble de unidades disponibles de las que realmente existen.',
    },
    unresolved: {
      title: 'Sin resolver',
      what:
        'Son variantes de Shopify que no tienen un SKU de BIMS equivalente conocido: BIMS no reconoce ese código.',
      howAutoResolves:
        'La reconciliación automática elimina la variante o pasa el producto a borrador si no se encuentra una correspondencia válida en BIMS, para evitar vender algo que no existe en el inventario real.',
      manualActions:
        'El botón "Ignorar" (con nota opcional) marca la variante como revisada sin tomar acción sobre Shopify, útil para casos excepcionales que el equipo ya conoce.',
      ifNothingDoneTitle: '¿Qué pasa si no se hace nada?',
      ifNothingDone:
        'El stock de esa variante nunca se actualiza: existe el riesgo de vender artículos que en realidad no están disponibles.',
    },
    mismatches: {
      title: 'Diferencias de nombre',
      what:
        'El SKU coincide entre Shopify y BIMS, pero el nombre del producto es distinto entre ambos sistemas.',
      howAutoResolves:
        'La reconciliación automática aplica el SKU de BIMS como fuente de verdad para mantener la sincronización de inventario, aunque el nombre visible en Shopify puede seguir siendo diferente.',
      manualActions:
        'El botón "Aprobar" confirma el SKU de BIMS para esa variante; "Ignorar" la deja marcada como revisada sin aplicar cambios.',
      ifNothingDoneTitle: '¿Qué pasa si no se hace nada?',
      ifNothingDone: 'Esa variante nunca se sincroniza correctamente entre BIMS y Shopify.',
    },
  },
}
