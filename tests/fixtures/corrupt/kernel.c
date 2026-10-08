/* Demo kernel for qemu-replay-mcp: a real memory-corruption bug.
 *
 * record_sample() has an off-by-one: it accepts slot == SLOTS, which writes
 * one element past history[] - straight into `magic`. kmain notices the
 * corrupted magic much later and crashes (triple fault). The crash site
 * tells you nothing about the cause; time travel finds the culprit write.
 */

#define SLOTS 8

struct state {
    unsigned int history[SLOTS];
    unsigned int magic;          /* must stay 0xC0FFEE */
};

struct state st = { {0}, 0xC0FFEE };
volatile unsigned int ticks;

struct __attribute__((packed)) idtr { unsigned short limit; unsigned int base; };

void record_sample(unsigned int slot, unsigned int value) {
    if (slot > SLOTS)            /* BUG: should be slot >= SLOTS */
        return;
    st.history[slot] = value;    /* CORRUPT_LINE */
}

void crash(void) {
    static const struct idtr empty = {0, 0};
    __asm__ volatile("lidt %0" : : "m"(empty));
    __asm__ volatile("ud2");
}

static void serial_puts(const char *s) {
    for (; *s; s++)
        __asm__ volatile("outb %0, %1" : : "a"(*s), "Nd"((unsigned short)0x3F8));
}

void kmain(void) {
    serial_puts("boot ok\n");
    for (unsigned int i = 0;; i++) {
        ticks++;
        record_sample(i % 16, i * 3);
        /* The check runs only every 1000 iterations, so the crash happens
         * long after the corrupting write. */
        if (i % 1000 == 999 && st.magic != 0xC0FFEE)
            crash();
    }
}
