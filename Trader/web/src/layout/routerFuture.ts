// React Router v7 behaviour opted into now, for every router (the app's and the tests'). It also silences
// React Router 6's "future flag" console warnings.
export const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true } as const;
