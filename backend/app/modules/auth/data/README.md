# Auth data files

## `common_passwords.txt`

Passwords rejected by the password policy, regardless of whether they satisfy
the composition rules.

- **Source:** [SecLists](https://github.com/danielmiessler/SecLists) —
  `Passwords/Common-Credentials/100k-most-used-passwords-NCSC.txt`
- **Origin:** published by the UK National Cyber Security Centre, derived from
  the Have I Been Pwned breach corpus.
- **Entries:** 99,840, one per line.
- **License:** SecLists is distributed under the MIT License. The file is
  included verbatim; see the NOTICE at the repository root for how third-party
  material is treated.

### Why 100k and not 10k

The Phase 1 plan asked for a top-10k list, but `password1234` — a password that
satisfies every composition rule and must be rejected — is absent from the 10k
list and present in this one. A list that misses obvious candidates gives false
assurance.

### Refreshing the list

```bash
curl -o backend/app/modules/auth/data/common_passwords.txt \
  https://raw.githubusercontent.com/danielmiessler/SecLists/master/Passwords/Common-Credentials/100k-most-used-passwords-NCSC.txt
```

The loader lowercases and strips each line, so the file needs no preprocessing.
It must stay free of comments: every non-empty line is treated as a password.
