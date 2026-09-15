export const env = {
  // Requests go through the Vite dev proxy (/api) unless an explicit URL is set.
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? '/api/v1',
} as const
