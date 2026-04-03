# Vigil AI Safety and Liability Disclaimer

**O.D.I.N. (Orchestrated Dispatch & Inventory Network)**

**Effective Date:** February 14, 2026
**Last Updated:** February 14, 2026

**Sublab 3DP** ("Company," "we," "us," or "our")
**Principal:** Shane Smith
**Contact:** sublab3dp@gmail.com

---

## 1. What Vigil AI Is

Vigil AI is an optional **quality monitoring tool** included in the O.D.I.N. software. It uses machine learning (ONNX model inference) to analyze camera frames from 3D printers and detect potential print failures, including:

- **Spaghetti failures** — extruded filament detaching and creating tangled masses
- **First layer defects** — poor adhesion or deformation on the initial layer
- **Detachment failures** — prints separating from the build plate mid-print

When a failure is detected with sufficient confidence (after multiple confirmation frames), Vigil AI can optionally auto-pause the print to reduce waste.

---

## 2. What Vigil AI Is NOT

**Vigil AI is NOT a safety device.** Specifically:

- It is **NOT** a fire detection or fire prevention system
- It is **NOT** a smoke detector, thermal sensor, or environmental monitor
- It is **NOT** a thermal runaway protection system
- It is **NOT** a substitute for human supervision of 3D printers
- It is **NOT** a certified safety system under any standard (UL, CE, ISO, IEC, or otherwise)
- It is **NOT** designed or tested to prevent injury, death, fire, or property damage
- It is **NOT** a replacement for your printer's built-in safety features (thermal runaway firmware, thermal fuses, etc.)

**Do not rely on Vigil AI to keep you, your property, or your printers safe.**

---

## 3. Accuracy Limitations

Vigil AI detection is **probabilistic and imperfect.** You should expect:

### 3.1 False Negatives (Missed Failures)

Vigil AI **will miss some failures.** The system may fail to detect a print failure due to:

- Camera angle, lighting conditions, or image quality
- Failure types not covered by the current model (the system only detects the three types listed above)
- Failures that develop gradually or in ways the model has not been trained on
- Obstructions in the camera view (enclosure walls, filament paths, other printers)
- Network or processing delays
- Model inference errors
- Camera feed interruptions or go2rtc connectivity issues

**A lack of detection does not mean a print is succeeding.**

### 3.2 False Positives (Incorrect Detections)

Vigil AI **will sometimes report failures that are not occurring.** The system may incorrectly flag:

- Normal print features (support structures, overhangs, bridges)
- Printer components visible in the frame
- Shadows, reflections, or lighting changes
- Intentional print geometries that resemble failure patterns

### 3.3 Confidence Thresholds

Detection confidence thresholds are configurable. Lower thresholds increase sensitivity but also increase false positives. Higher thresholds reduce false positives but increase the risk of missed detections. **There is no threshold setting that eliminates both false positives and false negatives.**

---

## 4. Auto-Pause Limitations

The auto-pause feature is provided on a **best-effort basis.** Auto-pause may fail to engage due to:

- The printer not responding to pause commands (firmware issue, network failure, protocol limitation)
- Delays between detection and command delivery
- The printer being in a state that does not accept pause commands
- The failure progressing faster than the detection-confirmation-pause cycle
- The detection being a false negative (failure not detected at all)
- Software crashes, restarts, or resource exhaustion on the host server
- The vision_monitor daemon being stopped, crashed, or unresponsive

**Auto-pause is a convenience feature, not a safety mechanism. It may not work when you need it most.**

---

## 5. Assumption of Risk

By enabling and using Vigil AI, you acknowledge and agree that:

1. **3D printing is inherently hazardous.** FDM/FFF printers operate at high temperatures (200-300+ C nozzle, 60-110 C bed). Resin printers involve UV-curing chemicals. Both present risks of fire, burns, equipment damage, toxic fumes, and injury.

2. **Unsupervised printing carries additional risks.** Operating printers without direct human supervision increases the likelihood that a failure, fire, or hazard will go unaddressed.

3. **You assume all risk** associated with operating your 3D printers, whether or not Vigil AI is enabled, and whether or not Vigil AI detects or fails to detect a failure.

4. **Vigil AI does not reduce the inherent risks of 3D printing.** It is a supplementary monitoring tool, not a risk mitigation system.

5. **You are solely responsible** for the safe operation of your printers, your facility, and the safety of anyone in proximity to your printers.

---

## 6. No Liability

**TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, SUBLAB 3DP, SHANE SMITH, AND THEIR AFFILIATES SHALL NOT BE LIABLE FOR ANY DAMAGES WHATSOEVER ARISING FROM OR RELATED TO THE USE OF VIGIL AI, INCLUDING BUT NOT LIMITED TO:**

- Fire, explosion, or thermal events involving 3D printers
- Damage to printers, print beds, nozzles, or other equipment
- Damage to property, buildings, or contents
- Personal injury, burns, or health effects (including from fumes or chemical exposure)
- Failed, wasted, or damaged prints
- Lost production time or business interruption
- Loss of data or digital assets
- Any harm resulting from Vigil AI's failure to detect a print failure
- Any harm resulting from Vigil AI incorrectly detecting a failure (false positive)
- Any harm resulting from the auto-pause feature failing to engage or engaging incorrectly

**THIS APPLIES WHETHER OR NOT SUBLAB 3DP WAS ADVISED OF THE POSSIBILITY OF SUCH DAMAGES AND REGARDLESS OF THE THEORY OF LIABILITY (CONTRACT, TORT, NEGLIGENCE, STRICT LIABILITY, PRODUCT LIABILITY, OR OTHERWISE).**

[ATTORNEY REVIEW RECOMMENDED: Enforceability of broad liability exclusions varies by jurisdiction. Some jurisdictions limit exclusion of liability for personal injury or do not allow exclusion of implied warranties for consumer products.]

---

## 7. Recommended Safety Practices

Vigil AI is one layer in a defense-in-depth approach to 3D printer safety. We **strongly recommend** the following safety practices regardless of whether you use Vigil AI:

### 7.1 Fire Safety

- Install **smoke detectors** in the room where printers operate
- Keep a **fire extinguisher** (Class C / electrical fire rated) accessible near your printers
- Consider **fire-rated printer enclosures** for unattended operation
- Ensure your electrical circuits are **properly rated** for the load of your printer fleet
- Do not use power strips or extension cords for printers; use dedicated circuits where possible
- Remove flammable materials from the vicinity of printers

### 7.2 Firmware and Hardware Safety

- Ensure your printers have **thermal runaway protection** enabled in firmware
- Keep printer firmware updated to the latest version from the manufacturer
- Inspect wiring, connectors, and heating elements regularly for wear
- Replace worn or damaged components (especially heating cartridges, thermistors, and wiring)
- Use quality power supplies rated for your printer's requirements

### 7.3 Operational Safety

- **Never leave printers completely unattended** for extended periods without additional safety measures
- Check on prints periodically, especially during the first layer
- Ensure adequate ventilation for printer operation (particularly ABS, ASA, and resin printers)
- Train all operators on emergency procedures, including emergency stop and power cutoff
- Know the location and operation of your building's fire suppression systems
- Have an evacuation plan for the printer room or facility

### 7.4 Camera and Monitoring Best Practices

If using Vigil AI:

- Position cameras to provide a clear, unobstructed view of the print area
- Ensure consistent lighting (avoid strong shadows or reflections)
- Test detection on non-critical prints before relying on it for production
- Periodically review detection history to calibrate confidence thresholds
- Do not disable camera feeds or Vigil AI monitoring without alternative supervision in place

---

## 8. No Safety Certification

Vigil AI has **not been tested, certified, or approved** by any safety standards organization, including but not limited to:

- UL (Underwriters Laboratories)
- CE / EU safety directives
- ISO / IEC safety standards
- OSHA / workplace safety standards
- NFPA (National Fire Protection Association)
- Any local fire marshal or building code authority

No claim is made that Vigil AI meets any safety standard or regulatory requirement.

---

## 9. Indemnification

You agree to indemnify, defend, and hold harmless Sublab 3DP, Shane Smith, and their affiliates from any and all claims, damages, losses, liabilities, costs, and expenses (including reasonable attorneys' fees) arising from or related to:

- Your use of Vigil AI
- Your reliance on Vigil AI detections or auto-pause functionality
- Any injury, damage, or loss resulting from your printer operations
- Any failure of Vigil AI to detect a print failure or prevent damage
- Claims by third parties arising from your use of Vigil AI in connection with your printer operations

---

## 10. Modifications

We may update this disclaimer to reflect changes in the Vigil AI system, applicable law, or best practices. Material changes will be communicated through release notes accompanying Software updates. Your continued use of Vigil AI after such updates constitutes acceptance of the modified disclaimer.

---

## 11. Severability

If any provision of this disclaimer is found to be unenforceable or invalid, the remaining provisions shall continue in full force and effect.

---

## 12. Contact

For questions about Vigil AI safety, to report a safety concern, or to provide feedback on detection accuracy:

**Sublab 3DP**
Shane Smith, Principal
Email: sublab3dp@gmail.com

---

**BY ENABLING VIGIL AI, YOU ACKNOWLEDGE THAT YOU HAVE READ, UNDERSTOOD, AND AGREE TO THIS DISCLAIMER. IF YOU DO NOT AGREE, DISABLE VIGIL AI IN YOUR O.D.I.N. SETTINGS.**

---

*[ATTORNEY REVIEW RECOMMENDED: This disclaimer should be reviewed by a qualified attorney with experience in product liability and software disclaimers. Key areas: enforceability of broad liability exclusions for physical harm, jurisdictional limitations on warranty disclaimers, and whether in-app presentation of this disclaimer is legally sufficient to establish acceptance.]*

*This document is provided as a template and does not constitute legal advice.*
