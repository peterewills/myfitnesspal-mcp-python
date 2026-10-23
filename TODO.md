# TODO

- **Wait for CI before deploying.** Railway can hold a GitHub-triggered deploy until GitHub Actions pass (`checkSuites` on deployment trigger `fae0a5d9-e072-445c-b87a-653697d3afbd`). Blocked on Actions running on the fork at all: no workflow run has been recorded for any push yet.
- **Recipes not yet logged in the app.** MFP creates a recipe's loggable food on first log, so we cannot log a recipe that was never logged in the app. Creating that food ourselves would need the app's undocumented call.
- **Saved meals.** `GET www.myfitnesspal.com/api/services/users/meals/mine` lists saved meals with their foods' descriptions and macros, but no food ids were seen; logging one needs more digging.
