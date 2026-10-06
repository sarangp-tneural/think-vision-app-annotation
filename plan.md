# Deploy Pipeline — Project Plan

## What we're building

Right now, when we train a new AI model for a client (using their annotated
footage/images in ThinkVision), getting that model onto the client's live server is
a manual, ad-hoc process. We're building a **guided, in-app deployment pipeline**
that handles this end-to-end, with a human approval step built in and a one-click
"undo" if something goes wrong.

In short: **annotate → train → test → approve → deploy → (roll back if needed)**,
all from inside ThinkVision, without anyone needing to touch a terminal.

## Why this matters

- **Faster turnaround** — no manual file-copying or ad-hoc scripts between our team
  and a client's server.
- **Safer deployments** — every model is tested and reviewed by a person before it
  ever reaches a client's live system. Nothing goes live automatically.
- **Reversible** — if an approved model underperforms once it's live, we can revert
  to the previous version with one click, instead of scrambling.
- **No leaked credentials** — server logins are entered fresh each time they're
  needed and are never stored anywhere, reducing security risk.
- **Auditable** — every run (who trained what, who approved it, when it was
  deployed) is recorded and visible in the app.

## How it will work, in plain terms

1. **Send the data.** The app packages up the client's annotated images and sends
   them securely to the server where training happens.
2. **Sanity-check first.** Before any training starts, the system checks the new
   data matches what's expected (right categories/labels) — if not, it stops early
   with a clear explanation instead of wasting time training on bad data.
3. **Train the model.** Training runs on the remote server; progress is visible
   live in the app.
4. **Bring the model back and test it.** Once training finishes, the new model is
   pulled back and tested against a held-out set of images, side-by-side with
   whatever model is currently live — so we can see, visually, whether it's
   actually better.
5. **Human review.** The reviewer gets notified, looks at the results in-app, and
   either **approves** or **rejects** the new model. Nothing ships without this
   step.
6. **Deploy.** Once approved, the model is pushed to the client's live server. The
   previous two versions are automatically kept as backups.
7. **Rollback, if ever needed.** A visible "restore previous version" button is
   always available — no need to involve engineering to undo a bad deployment.

There are three flavors of this flow depending on where a client project is at:
a first-time **bootstrap** run (just enough data to help speed up labeling the
rest), a **first full production** run, and later **merge** runs (adding new
batches of footage over time). The app figures out automatically which one
applies and tells the user before starting.

## What's staying the same

- Nothing about the existing annotation, review, or in-app training features
  changes — this is purely additive.
- Today's simple "activate a model for auto-labeling" feature keeps working
  as-is; the new pipeline builds on top of it rather than replacing it.

## Rough sequence of work

We're building this in dependency order — each piece unlocks the next:

1. Secure connection to remote training servers (foundation for everything else)
2. Packaging and validating the dataset before it's sent
3. Data-consistency check (catch mismatched labels early)
4. Sending the data to the training server
5. Remote training itself, with live progress
6. Bringing the trained model back and registering it
7. Automated testing + side-by-side comparison + reviewer notification
8. Approval step, live deployment, and backup rotation (the highest-care part of
   the whole build, given it touches a client's live server)
9. One-click rollback
10. The in-app wizard that ties all of the above together for end users

We are not attaching calendar estimates to this list yet — it's ordered by what
depends on what, and effort will get scoped once we're a few steps in and have a
better feel for how the remote-server pieces go in practice.

## What's explicitly not in scope right now

- We are **not** building a fully automated, no-human-review deployment path —
  approval will always be a required manual step.
- We are **not** overhauling how background jobs are tracked app-wide; we're only
  adding the one safety check needed to make sure two deployments can't run into
  each other on the same client server at the same time.

*(A detailed, engineering-level version of this plan — file-by-file — lives in
`ROADMAP.md` in the same repository.)*
