// stimulator.ino — one spine press per command, labelled with a barcode and an
// onset pulse on the sync pin (sync pin -> BNC -> spare EScope input).
//
// Wiring: servo signal -> pin 9 (brown GND, red 5V). Sync: pin 7 -> BNC centre,
// Uno GND -> BNC shield.
//
// ---------------------------------------------------------------------------
// Commands (one line of plain text each, ending in a newline):
//
//   R,<rest_us>,<ramp_ms>
//       Move to the rest position. The very first R after start-up jumps there
//       (the servo has been limp), so do it with the probe away from the leg.
//       Later R commands move there smoothly over ramp_ms.
//
//   P,<trial>,<spine>,<rest_us>,<amount_us>,<push_ms>,<hold_ms>,<return_ms>
//       One press. The servo must already be at rest_us (send R first).
//       spine is the spine's number code (0-255; the laptop keeps the table).
//       Sequence: barcode (trial + spine) -> wait LEAD_MS from the barcode start
//       -> onset pulse + push by amount_us over push_ms -> hold for hold_ms
//       (timed from the END of the push) -> return to rest over return_ms.
//       amount_us can be negative to push the other way.
//
//   ?   report status          X   abort the press that is running
//
// Replies:
//   ok,<trial>,<spine>               command received and accepted
//   done,<trial>,<spine>,<lead_us>,<push_us>,<hold_us>,<return_us>   measured durations
//   ok,R,<rest_us> / done,R,<rest_us>
//   aborted,<trial>     err,<reason>     status,<attached 0/1>,<position_us>
//
// All values are raw servo units (pulse width in us, time in ms). The laptop
// converts them to mm and mm/s using the calibration.
// ---------------------------------------------------------------------------

#include <Servo.h>
#include <stdlib.h>
#include <string.h>

// ----------------------------- settings ------------------------------------
const int SERVO_PIN = 9;
const int SYNC_PIN  = 7;

// Start-up behaviour (still undecided). true: the servo stays limp until the
// first R command. false: it goes to SAFE_US as soon as the board starts.
const bool START_LIMP = true;
const int  SAFE_US    = 1500;

const int MIN_US = 600;    // pulse-width limits; never command outside these
const int MAX_US = 2400;   // (the Servo library also caps at 544-2400 us)

const unsigned long LEAD_MS  = 3000;  // barcode start -> push onset
const unsigned long BIT_MS   = 5;     // length of one barcode bit
const int           TRIAL_BITS = 16;  // trial numbers 0-65535
const int           SPINE_BITS = 8;   // spine codes 0-255
const unsigned long ONSET_MS = 5;     // length of the onset pulse

const unsigned long MAX_PUSH_MS   = 10000;
const unsigned long MAX_HOLD_MS   = 60000;
const unsigned long MAX_RETURN_MS = 10000;
// ---------------------------------------------------------------------------

Servo servo;
bool attached = false;
int  posUs = SAFE_US;          // last commanded pulse width

bool abortFlag = false;
bool onsetActive = false;
unsigned long onsetStart = 0;  // micros() when the onset pulse went high

// Command the servo, always inside the limits.
void setPos(int us) {
  posUs = constrain(us, MIN_US, MAX_US);
  servo.writeMicroseconds(posUs);
}

// Called inside every waiting loop during a press: ends the onset pulse on
// time, and listens for an X (abort).
void service() {
  if (onsetActive && micros() - onsetStart >= ONSET_MS * 1000UL) {
    digitalWrite(SYNC_PIN, LOW);
    onsetActive = false;
  }
  while (Serial.available()) {
    if (Serial.read() == 'X') abortFlag = true;
  }
}

// Wait, while servicing. Stops early on abort.
void waitMs(unsigned long ms) {
  unsigned long t0 = micros();
  while (micros() - t0 < ms * 1000UL) {
    service();
    if (abortFlag) return;
  }
}

// Move from one pulse width to another in a straight line over durMs.
// The servo itself only reads a new position every 20 ms, so very short
// ramps are limited by the servo's own top speed.
void ramp(int fromUs, int toUs, unsigned long durMs, bool abortable) {
  if (durMs > 0) {
    unsigned long dur = durMs * 1000UL;
    unsigned long t0 = micros();
    while (true) {
      unsigned long elapsed = micros() - t0;
      if (elapsed >= dur) break;
      setPos(fromUs + (int)((toUs - fromUs) * ((float)elapsed / dur)));
      service();
      if (abortable && abortFlag) return;
    }
  }
  setPos(toUs);
}

// Level-coded barcode: slot 0 = start bit (high), slot 1 = guard (low),
// then 16 bits of trial number and 8 bits of spine code, most significant bit
// first (high = 1). Each slot lasts BIT_MS. Timed from one start point so
// errors don't add up.
void sendBarcode(unsigned int trial, unsigned int spine) {
  const int nBits  = TRIAL_BITS + SPINE_BITS;
  const int nSlots = nBits + 2;
  unsigned long code = ((unsigned long)trial << SPINE_BITS) | spine;
  unsigned long slotUs = BIT_MS * 1000UL;
  unsigned long t0 = micros();
  for (int slot = 0; slot < nSlots; slot++) {
    int level;
    if (slot == 0)      level = HIGH;
    else if (slot == 1) level = LOW;
    else                level = (code >> (nBits - 1 - (slot - 2))) & 1;
    while (micros() - t0 < (unsigned long)slot * slotUs) {}
    digitalWrite(SYNC_PIN, level);
  }
  while (micros() - t0 < (unsigned long)nSlots * slotUs) {}
  digitalWrite(SYNC_PIN, LOW);
}

void doPress(unsigned int trial, unsigned int spine, int restUs, int amountUs,
             unsigned long pushMs, unsigned long holdMs, unsigned long returnMs) {
  abortFlag = false;

  unsigned long tBarcode = micros();
  sendBarcode(trial, spine);
  while (micros() - tBarcode < LEAD_MS * 1000UL) {
    service();
    if (abortFlag) {                       // nothing has moved yet
      Serial.print("aborted,"); Serial.println(trial);
      return;
    }
  }

  // Onset pulse marks the start of the push.
  digitalWrite(SYNC_PIN, HIGH);
  unsigned long tOnset = micros();
  onsetStart = tOnset;
  onsetActive = true;

  ramp(restUs, restUs + amountUs, pushMs, true);
  unsigned long tPushEnd = micros();

  if (!abortFlag) waitMs(holdMs);
  unsigned long tHoldEnd = micros();

  ramp(posUs, restUs, returnMs, false);   // always return, even after an abort
  unsigned long tEnd = micros();

  while (onsetActive) service();          // make sure the onset pulse ended

  if (abortFlag) {
    Serial.print("aborted,"); Serial.println(trial);
    return;
  }
  Serial.print("done,");   Serial.print(trial);
  Serial.print(",");       Serial.print(spine);
  Serial.print(",");       Serial.print(tOnset - tBarcode);
  Serial.print(",");       Serial.print(tPushEnd - tOnset);
  Serial.print(",");       Serial.print(tHoldEnd - tPushEnd);
  Serial.print(",");       Serial.println(tEnd - tHoldEnd);
}

// Split "P,1,2,3" into numbers after the letter. Returns how many were found.
int parseNumbers(char *line, long *out, int maxN) {
  int n = 0;
  char *tok = strtok(line, ",");          // the command letter
  while ((tok = strtok(NULL, ",")) != NULL && n < maxN) {
    char *end;
    out[n] = strtol(tok, &end, 10);
    if (end == tok || *end != '\0') return -1;   // not a number
    n++;
  }
  return n;
}

void handleLine(char *line) {
  char cmd = line[0];
  long v[7];

  if (cmd == '?') {
    Serial.print("status,"); Serial.print(attached ? 1 : 0);
    Serial.print(",");       Serial.println(posUs);

  } else if (cmd == 'R') {
    if (parseNumbers(line, v, 2) != 2) { Serial.println("err,R needs rest_us and ramp_ms"); return; }
    if (v[0] < MIN_US || v[0] > MAX_US) { Serial.println("err,rest outside limits"); return; }
    if (v[1] < 0 || v[1] > (long)MAX_RETURN_MS) { Serial.println("err,ramp_ms out of range"); return; }
    Serial.print("ok,R,"); Serial.println(v[0]);
    if (!attached) {                      // first move: jump (servo was limp)
      setPos((int)v[0]);
      servo.attach(SERVO_PIN);
      attached = true;
    } else {
      ramp(posUs, (int)v[0], (unsigned long)v[1], false);
    }
    Serial.print("done,R,"); Serial.println(posUs);

  } else if (cmd == 'P') {
    if (parseNumbers(line, v, 7) != 7) { Serial.println("err,P needs 7 numbers"); return; }
    long trial = v[0], spine = v[1], rest = v[2], amount = v[3];
    long pushMs = v[4], holdMs = v[5], returnMs = v[6];
    if (trial < 0 || trial > 65535)               { Serial.println("err,trial must be 0-65535"); return; }
    if (spine < 0 || spine > 255)                 { Serial.println("err,spine must be 0-255"); return; }
    if (!attached)                                { Serial.println("err,send R first"); return; }
    if (rest != posUs)                            { Serial.println("err,not at rest; send R first"); return; }
    if (rest + amount < MIN_US || rest + amount > MAX_US) { Serial.println("err,push outside limits"); return; }
    if (pushMs < 0 || pushMs > (long)MAX_PUSH_MS) { Serial.println("err,push_ms out of range"); return; }
    if (holdMs < 0 || holdMs > (long)MAX_HOLD_MS) { Serial.println("err,hold_ms out of range"); return; }
    if (returnMs < 0 || returnMs > (long)MAX_RETURN_MS) { Serial.println("err,return_ms out of range"); return; }
    Serial.print("ok,"); Serial.print(trial); Serial.print(","); Serial.println(spine);
    doPress((unsigned int)trial, (unsigned int)spine, (int)rest, (int)amount,
            (unsigned long)pushMs, (unsigned long)holdMs, (unsigned long)returnMs);

  } else if (cmd == 'X') {
    Serial.println("err,no press running");

  } else {
    Serial.println("err,unknown command");
  }
}

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(100);
  pinMode(SYNC_PIN, OUTPUT);
  digitalWrite(SYNC_PIN, LOW);
  if (!START_LIMP) {
    setPos(SAFE_US);
    servo.attach(SERVO_PIN);
    attached = true;
  }
  Serial.println("ready,roach-touch-stimulator,1");   // the app checks this name
}

void loop() {
  if (!Serial.available()) return;
  char line[64];
  int n = Serial.readBytesUntil('\n', line, sizeof(line) - 1);
  line[n] = '\0';
  if (n > 0 && line[n - 1] == '\r') line[--n] = '\0';
  if (n == 0) return;
  handleLine(line);
}
