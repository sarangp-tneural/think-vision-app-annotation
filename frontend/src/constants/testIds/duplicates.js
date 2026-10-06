// Test IDs for duplicate-image handling (upload warning + duplicates dialog).
// See ./index.js for the recipe to add a new feature file.

export const DUPLICATES = {
  openButton: 'duplicates-btn',
  dialog: 'duplicates-dialog',
  group: (i) => `duplicates-group-${i}`,
  deleteButton: (id) => `duplicates-delete-${id}`,
  empty: 'duplicates-empty',
  unhashedNote: 'duplicates-unhashed-note',
};
