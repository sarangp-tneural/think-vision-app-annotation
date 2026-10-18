// Test IDs for the profile page (account info, name, password, avatar, stats).
// Keys are camelCase, values are kebab-case `<feature>-<element>`; see ./index.js.

export const PROFILE = {
	page: 'profile-page',
	navbarLink: 'navbar-profile-link',
	avatar: 'profile-avatar',
	avatarInput: 'profile-avatar-input',
	avatarUploadButton: 'profile-avatar-upload-button',
	avatarRemoveButton: 'profile-avatar-remove-button',
	email: 'profile-email',
	role: 'profile-role',
	memberSince: 'profile-member-since',
	nameInput: 'profile-name-input',
	nameSaveButton: 'profile-name-save-button',
	currentPasswordInput: 'profile-current-password-input',
	newPasswordInput: 'profile-new-password-input',
	confirmPasswordInput: 'profile-confirm-password-input',
	passwordError: 'profile-password-error',
	passwordSaveButton: 'profile-password-save-button',
	stat: (key) => `profile-stat-${key}`,
};
