# OSHWA Certification Plan: RETINA Node

Plan for getting OSHWA open source hardware certification for the RETINA passive radar node.

## Scope and decisions

- **What gets certified:** the RETINA node, meaning its hardware plus the software that runs on it. Server-side repos (retina-server, geolocator, analytics, custody, tower-finder-service and so on) are out of scope.
- **Hardware:** every part is bought off the shelf. Offworld's contribution is the choice of parts, integration, assembly and software.
- **Licenses:**
  - Hardware and design files: CERN-OHL-P-2.0
  - Software: MIT
  - Documentation: CC-BY-4.0

  OSHWA has already accepted this combination on other projects, for example US002698.
- **Export control:** Offworld has filed nothing. Another company plans to file for its own turnkey kit.

## What OSHWA requires

These points were checked against OSHWA's public application form source ([oshwa/oshwa-certification-form](https://github.com/oshwa/oshwa-certification-form)) and its public certification data. certification.oshwa.org itself could not be reached from this environment, so requirement wording taken from that site is from search snippets.

- **Openness:** everything under the creator's control (parts, designs, code and rights) must be open. Closed third-party parts are allowed if they are clearly identified and their datasheets can be obtained without an NDA.
- **Software:** all software "necessary for the operation of your hardware" must be under an OSI-approved license.
- **Documentation:** docs and design files must be public, in the format you would use to edit them, and openly licensed.
- **Creator contribution:** the applicant must affirm that they meet the "creator contribution" requirement. OSHWA describes this as "intentionally flexible".
- **Cost and renewal:** certification is free. It is self-certified, renewed every year, and each version gets its own UID (e.g. `US00xxxx`). Reviews usually take less than two weeks.
- **Export control:** the form has no export-control questions, but certifying lists the project publicly as open hardware.
- **Contact:** certification@oshwa.org

## Main risks

1. **Off-the-shelf only.** Every certified project we found that uses mostly off-the-shelf parts still includes some hardware its creators designed (panels, frames, printed parts). We found no example where the creator's only contribution is integration and software. OSHWA may decide there is no open *hardware* to certify.
2. **Closed SDRplay API.** The RSPduo only works with SDRplay's closed binary API. That conflicts with the rule that all software needed to operate the hardware be OSI-licensed, although OSHWA also exempts third-party closed parts "outside of [the creator's] control". Nothing we found settles which rule wins. The OS image also bundles SDRconnect, which is proprietary.
3. **Export control.** The March 2026 counsel memo says this class of system may fall under ITAR USML XI(a)(3)(xxvii) or several EAR ECCNs, and recommends a Commodity Jurisdiction or classification ruling. A ruling for another company's kit may not cover RETINA. OSHWA certification publicises the hardware as open for any use, anywhere.

Risks 1 and 2 are settled by asking OSHWA (Phase 0). Risk 3 needs counsel.

## Phase 0: Confirm eligibility (do this first)

1. Email certification@oshwa.org and describe the node:
   - Pi 5 + RSPduo + Yagi antennas + enclosure, all off the shelf.
   - Offworld supplies the open BOM, assembly and wiring docs, OS image and application software.
   - The RSPduo needs the closed SDRplay API.

   Ask two questions:
   - Does integration, documentation and software meet the creator-contribution requirement?
   - Is a closed vendor driver for an off-the-shelf module acceptable?
2. Ask counsel (Thomsen and Burke) whether publicly certifying and publishing the node as open hardware changes anything under ITAR or EAR. Also ask whether the other company's filing covers RETINA or whether Offworld needs its own CJ or CCATS.
3. **Stop here if either answer is no.** Options at that point:
   - If creator contribution is the problem: add an Offworld-designed part, such as an enclosure, mount or antenna, as the certified hardware.
   - If the SDRplay API is the problem: support an SDR with open drivers.

   Both options add scope, so they need a separate decision.

## Phase 1: Licensing cleanup (node repos)

| Repo | Current | Action |
|---|---|---|
| owl-os | MIT, "(c) 2020 Matthias Lüscher" (upstream edi-pi) | Keep. Add an Offworld Labs copyright line for Offworld's changes |
| retina-node (incl. config-merger) | none | Add MIT LICENSE |
| retina-gui | none | Add MIT LICENSE |
| retina-telemetry | none | Add MIT LICENSE |
| retina-spectrum | none | Add MIT LICENSE |
| retina-tracker | MIT, no copyright holder named | Fill in the copyright holder |
| blah2-arm | MIT (30hours, upstream) | Keep. Add an Offworld copyright line if Offworld has made changes |
| adsb2dd | MIT (30hours, upstream) | Keep, same as above |
| tar1090-node | GPL-2.0-or-later (upstream) | Keep (OSI-approved) |

- Before adding MIT to a repo, check that it contains no third-party code under an incompatible license. Also check that everyone who has committed to it agrees to the license, including contractors.
- Check the SDRplay API license. owl-os downloads the installer and SDRconnect from an Offworld R2 bucket (`plugins/playbooks/os_setup/versions.yml`), so confirm that SDRplay allows this redistribution.
- Check the licenses of the Docker base images and the main dependencies of each node container. They must all be OSI-approved, apart from the SDRplay parts covered in Phase 0.

## Phase 2: Hardware documentation

Create a new public repo (e.g. `offworldlabs/retina-hardware`), licensed CERN-OHL-P-2.0 for hardware files and CC-BY-4.0 for docs, containing:

1. **BOM:** each part's name, manufacturer and part number, supplier link, quantity, datasheet link, and whether it is open or proprietary. Parts:
   - RSPduo
   - Raspberry Pi 5
   - 35 W USB-C power supply
   - Heatsink/case
   - 64 GB high-endurance microSD
   - USB-A to USB-C cable
   - Enclosure
   - 2x Yagi antennas
   - Coax and adapters, mast hardware

   Mark the RSPduo, the Pi 5 and the SDRplay API as third-party proprietary. Confirm each datasheet is public and needs no NDA.
2. **Wiring and integration diagram:** antenna to RSPduo ports (reference and surveillance channels), RSPduo to Pi, power, and network.
3. **Assembly guide with photos** at each stage: enclosure fit-out, cable routing, antenna assembly and pointing (reference antenna toward the transmitter, surveillance antenna toward the coverage area), and mast mounting.
4. **Installation and siting guide:** antenna separation, height, choosing a tower, and a link to the tower-finder feature in retina-gui.
5. **Software bring-up:** link to the owl-os README (flashing, WiFi setup) and retina-node (deploy), including the no-Mender route in `retina-node/STANDALONE.md`.
6. **Versioning:** tag a hardware release (e.g. `hw-v1.0`) that pins the owl-os and retina-node versions it was documented against.

## Phase 3: Apply

1. Choose the **responsible party**: Offworld Lab LLC, with someone authorised to sign for it.
2. Fill in the form:
   - Project name "RETINA Node" and version `hw-v1.0`.
   - Description and intended use.
   - Documentation URL: the hardware repo.
   - Citations: blah2, adsb2dd, tar1090, edi-pi.
   - Licenses: CERN-OHL-P-2.0 / MIT / CC-BY-4.0.
3. Work through the licensing checklist. For any item you can't check, write an explanation that refers to the Phase 0 answer on the SDRplay API.
4. Submit, answer reviewer questions, and receive the UID.

## Phase 4: After certification

- Put the mark and UID in the hardware repo README, on the landing page, and on the product label if kits are sold. Follow OSHWA's mark-usage rules and don't modify the mark.
- Answer OSHWA's renewal email each year.
- Certify a new version (with its own UID) when hardware changes in a way that affects the docs, such as a different SDR, antenna or enclosure.

## Unrelated finding

`offworldlabs/docs` is a public repo containing `Passive-Radar-Memo_Final.pdf`, which is marked "Confidential / Attorney-Client Privilege / Attorney Work Product". Publishing it may waive privilege. Check with counsel whether it should stay public.
