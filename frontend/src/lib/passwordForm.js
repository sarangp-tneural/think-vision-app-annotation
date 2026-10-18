export const MIN_PASSWORD_LEN = 8; // keep in sync with backend/routers/profile.py

// Returns an error message, or null when the form can be submitted.
export function validatePasswordChange({ current, next, confirm }) {
  if (!current) return "Enter your current password";
  if (next.length < MIN_PASSWORD_LEN) return `New password must be at least ${MIN_PASSWORD_LEN} characters`;
  if (next === current) return "New password must be different from the current one";
  if (next !== confirm) return "New passwords do not match";
  return null;
}
