## What and why

<!-- What this pull request changes, and the problem it solves. Link the issue: "Fixes #123". -->

## How it was tested

<!-- The tests you added or ran, and anything you checked by hand (screens: light and dark, keyboard, screen reader). -->

## Checklist

- [ ] Every commit is signed off (`git commit -s`, CONTRIBUTING.md "Sign your commits").
- [ ] Tests cover the change, and the gate for the area I touched passes.
- [ ] A model change has its migration; no applied migration was edited.
- [ ] New endpoints declare their minimum role; writes go through an audited service.
- [ ] No secret appears in a response, an audit event, a log or an export.
- [ ] The docs that describe this change are updated (README, docs/, runbooks).
- [ ] A new dependency, if any, is named here with the reason.
