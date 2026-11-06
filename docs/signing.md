# Signing Yarn with the Apple Developer Program

The US$99 fee does not skip the first Allow. It makes Yarn one app, so the next install does not send you through Privacy & Security again, and a downloaded zip opens without a Gatekeeper warning.

Microphone permission belongs to `ma-capture`. Call audio belongs to `Yarn.app`. The fee stabilises `Yarn.app`. The first headphone call on a new Mac still needs Screen & System Audio Recording turned on once.

## Before the membership is active

`bin/yarn-release` and `bin/yarn-dock` keep signing ad-hoc. Setup still clears the call-audio grant on those builds, because an ad-hoc signature changes every compile and macOS then hides Yarn from the list.

## The day the membership is active

1. Enrol as an individual at [developer.apple.com/programs/enroll](https://developer.apple.com/programs/enroll/). Use the Apple ID you want on the certificate, with two-factor authentication, and a card in your own name. The certificate shows that legal name. A company name needs a separate organisation enrolment and a D-U-N-S number.
2. Xcode → Settings → Accounts → the Apple ID → Manage Certificates → **Developer ID Application**. Account Holder is the role that can create it.
3. Confirm the login keychain has exactly one:

   ```bash
   security find-identity -v -p codesigning | grep "Developer ID Application"
   ```

   If more than one appears, releases refuse to guess. Set `YARN_CODESIGN_IDENTITY` to the full name in quotes.
4. Create an app-specific password at appleid.apple.com, then store the notary credentials once:

   ```bash
   xcrun notarytool store-credentials yarn-notary \
     --apple-id "YOUR_APPLE_ID" \
     --team-id "YOUR_TEAM_ID" \
     --password "YOUR_APP_SPECIFIC_PASSWORD"
   ```

5. Build the zip:

   ```bash
   bin/yarn-release
   ```

   `bin/yarn-sign` finds the certificate, signs `Yarn Setup.app` and `Yarn.app` with the hardened runtime and a timestamp, then notarizes the zip. `YARN_SKIP_NOTARIZE=1` signs and stops, which is only for checking the certificate before the notary profile exists.

6. Install that build once. Setup sees `Developer ID Application` on `Yarn.app` and leaves the call-audio grant in place. The first Allow on that Mac still has to be clicked. Later rebuilds with the same certificate do not wipe it.

## The day you publish

The Yarn page downloads `releases/latest`. That zip opens Yarn Setup, which is signed and notarized. The program is inside the app, because macOS can open a download from a temporary folder and a folder beside the app would be left behind.

1. Enrol, create the Developer ID Application certificate, and store the notary profile above.
2. `bin/yarn-release` signs and notarizes `dist/Yarn.Setup.Apple.Silicon.zip`.
3. Upload that zip as the new GitHub release.

`README.md` and `docs/index.html` tell people to double-click Yarn Setup. Those lines stay only while the file on `releases/latest` is this notarized zip. An ad-hoc zip is still blocked on first open, and the note on the page would then be wrong.

## What still asks

A room recording asks for the microphone, once, for `ma-capture`. A headphone call also asks Yarn.app for Screen & System Audio Recording, once per Mac. Notarization does not pre-tick either switch.
