import busio
import digitalio

REG_TXBYTES   = 0xFA
REG_MARCSTATE = 0xF5
MARCSTATE_IDLE = 0x01
STROBE_SIDLE  = 0x36
STROBE_SFTX   = 0x3B
STROBE_SCAL   = 0x33
STROBE_STX    = 0x35
TXFIFO_BURST  = 0x7F

CC1101_CONFIG = [
    (0x06, 120),   # PKTLEN - 7 words at 26 bits per word, repeated 5 times with a gap of 12 zeros and buffered to the next byte
    (0x07, 0x00),  # PKTCTRL1 - do not append status
    (0x08, 0b00),  # PKTCTRL0 - turn off data whitening, use FIFOs for TX, disable CRC calculation, and use fixed packet length mode
    (0x0D, 0x0C),  # FREQ2 - frequency = (FREQ2:FREQ1:FREQ0) × 26MHz / 2^16
    (0x0E, 0x1D),  # FREQ1 - 0x0C1D89 = 793,993 decimal
    (0x0F, 0x89),  # FREQ0 - 793,993 × 26,000,000 / 65,536 = 314.973 MHz
    (0x10, 0b11110110),  # MDMCFG4, set DRATE_E = 6
    (0x11, 0x83),  # MDMCFG3, set DRATE_M = 131, (256+DRATE_M) × 2^DRATE_E × 26MHz / 2^28 = 2400 baud
    (0x12, 0x30),  # MDMCFG2 - ASK/OOK modulation format, no preamble, no manchester encoding
    (0x13, 0x00),  # MDMCFG1 - set preamble length to zero, and set channel spacing to zero
    (0x17, 0x00),  # MCSM1 - after tx done, go to IDLE
    (0x18, 0x04),  # MCSM0 - enable autocal
    (0x21, 0x56),  # FREND1
    (0x22, 0x11),  # FREND0 - PA_POWER = 001 = use PATABLE index 1 for a 1 bit
    (0x23, 0xEA),  # FSCAL3 - The important parts are bits 4-7, enable charge pump calibration and use SmartRF Studio value for band-specific calibration configuration
    (0x2E, 0x09),  # TEST0 - Disable VCO selection calibration, required for the 300–348 MHz band
]

# OOK Settings
# FIFO tx access mode: burst
# PA table:index 0 = off, index 1 = on
FIFO_TX_ACCESS_MODE = [0x7E]
PATABLE = [0x00, 0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]


def configure(sck_pin, mosi_pin, miso_pin, csn_pin):
    spi = busio.SPI(sck_pin, MOSI=mosi_pin, MISO=miso_pin)
    cs = digitalio.DigitalInOut(csn_pin)
    cs.direction = digitalio.Direction.OUTPUT
    cs.value = True

    while not spi.try_lock():
        pass
    spi.configure(baudrate=4000000, polarity=0, phase=0)

    _configure_cc1101(spi,cs)

    return (spi, cs) 


def get_packet(serial, cmd1, cmd2, err1, err2):
    serial1 = (serial >> 16) & 0xFF  # First 2 bytes
    serial2 = (serial >> 8)  & 0xFF  # Middle 2 bytes
    serial3 =  serial        & 0xFF  # Last 3 bytes

    signal = _signal_from_words(serial1=serial1, serial2=serial2, serial3=serial3, cmd1=cmd1, cmd2=cmd2, err1=err1, err2=err2)
    return _packet_from_signal(signal)


def send(packet, spi, cs):
    """
    Transmits a packet over SPI via the CC1101.

    Args:
        packet: List of 120 integers (0-255) representing the packet bytes.
        spi:    busio.SPI instance, must already be locked and configured.
        cs:     digitalio.DigitalInOut chip select pin. Active low.
    """
    print("Sending...")
    # print("Packet length:", len(packet))
    success = _transmit(spi, cs, bytes(packet))
    print("Done" if success else "Failed")


def _configure_cc1101(spi, cs):
    config = CC1101_CONFIG

    # Reset first
    _send_strobe(spi, cs, 0x30)  # SRES

    # Write registers
    for reg, value in config:
        cs.value = False
        spi.write(bytes([reg, value]))
        cs.value = True

    # TX Settings
    cs.value = False
    spi.write(bytes(FIFO_TX_ACCESS_MODE))  # set FIFO tx mode
    spi.write(bytes(PATABLE))  # set PATABLE
    cs.value = True

    _send_strobe(spi, cs, STROBE_SIDLE)
    _send_strobe(spi, cs, STROBE_SFTX)
    _send_strobe(spi, cs, STROBE_SCAL)

    print("CC1101 configured for 314.973 MHz OOK at 2400 baud")


def _send_strobe(spi, cs, strobe):
    cs.value = False
    spi.write(bytes([strobe]))
    cs.value = True

def _read_status_register(spi, cs, reg):
    """
    Read a CC1101 status register over SPI.

    Status registers require both the read bit (7) and burst bit (6) to be set
    in the header byte (reg | 0xC0), as per the CC1101 datasheet.

    Args:
        spi: busio.SPI instance, must already be locked and configured.
        cs:  digitalio.DigitalInOut chip select pin. Active low.
        reg: Status register address (e.g. 0xF5 for MARCSTATE, 0xFA for TXBYTES).

    Returns:
        Single byte value of the register as an int (0-255).
    """
    cs.value = False
    spi.write(bytes([reg | 0xC0]))
    result = bytearray(1)
    spi.readinto(result)
    cs.value = True
    return result[0]

def _write_fifo(spi, cs, data):
    """
    Burst write bytes into the CC1101 TX FIFO.

    Sends the TXFIFO burst write address (0x7F = 0x3F | 0x40) followed by
    the data bytes in a single CS-asserted transaction. The FIFO holds a
    maximum of 64 bytes — caller is responsible for ensuring data fits in
    the available space to avoid TX FIFO overflow.

    Args:
        spi:  busio.SPI instance, must already be locked and configured.
        cs:   digitalio.DigitalInOut chip select pin. Active low.
        data: bytes or bytearray to write into the TX FIFO.
    """
    cs.value = False
    spi.write(bytes([TXFIFO_BURST]))
    spi.write(data)
    cs.value = True


def _transmit(spi, cs, tx_buf):
    # Clean start
    _send_strobe(spi, cs, STROBE_SIDLE)  # Need chip in IDLE
    _send_strobe(spi, cs, STROBE_SFTX)  # Flush TX FIFO

    # Prime FIFO with first 64 bytes
    first_chunk = tx_buf[:64]
    _write_fifo(spi, cs, first_chunk)
    tx_pos = len(first_chunk)

    # Start transmitting
    _send_strobe(spi, cs, STROBE_STX)
    
    # Refill FIFO as it drains
    while tx_pos < len(tx_buf):
        txbytes = _read_status_register(spi, cs, REG_TXBYTES) & 0x7F  # mask to bits 6:0
        underflow = _read_status_register(spi, cs, REG_TXBYTES) & 0x80  # mask to bit 7
        if underflow:
            print("TX underflow error")
            _send_strobe(spi, cs, STROBE_SIDLE)
            _send_strobe(spi, cs, STROBE_SFTX)
            return False
        free = 64 - txbytes
        if free > 0:
            chunk = tx_buf[tx_pos:tx_pos + free]
            _write_fifo(spi, cs, chunk)
            tx_pos += len(chunk)

    # Wait for FIFO to drain completely
    while True:
        state = _read_status_register(spi, cs, REG_MARCSTATE) & 0x1F  # MARCSTATE state bits are 4:0
        if state == MARCSTATE_IDLE:
            break

    _send_strobe(spi, cs, STROBE_SIDLE)
    return True


def _signal_from_words(serial1, serial2, serial3, cmd1, cmd2, err1, err2):
    """
    Builds a Proflame 2 signal from 7 hex word values.

    Each word structure (before Manchester encoding):
      1 + [8 data bits] + [padding bit] + [parity bit] + 1  = 12 bits
    After Manchester encoding: 24 bits
    With leading '11': 26 bits per word
    Total signal: 7 x 26 = 182 bits
    """
    words = [serial1, serial2, serial3, cmd1, cmd2, err1, err2]
    signal_bits = ''

    for i, word in enumerate(words):
        # Extract 8 data bits manually using bit shifting
        data_bits = ''.join('1' if (word >> (7 - b)) & 1 else '0' for b in range(8))

        # Padding bit: 1 for the first word, 0 for all others
        padding = 1 if i == 0 else 0

        # Parity bit over the 8 data bits + padding bit
        num_ones = data_bits.count('1') + padding
        parity = num_ones % 2

        # Build raw 12-bit word: 1 + data(8) + padding + parity + 1
        raw_bits = '1' + data_bits + str(padding) + str(parity) + '1'

        # Manchester encode each bit: 1 -> '10', 0 -> '01'
        manchester = ''.join('10' if b == '1' else '01' for b in raw_bits)

        # Prepend the literal '11' header (not encoded)
        signal_bits += '11' + manchester

    return signal_bits


def _packet_from_signal(signal):
    """
    Builds a 120-byte packet from a binary signal string.

    The signal is repeated 5 times, separated by 12 zero bits,
    then zero-padded to exactly 120 bytes (960 bits).

    Returns a list of 120 integers (0-255), one per byte.
    """
    gap = '0' * 12

    # Join 5 repetitions with the gap between each
    repeated = signal
    for _ in range(4):
        repeated += gap + signal

    # print("Repeated length in bits:", len(repeated))

    # Pad to 120 bytes (960 bits) with trailing zeros
    target_bits = 120 * 8
    padded = repeated + '0' * (target_bits - len(repeated))

    # print("Padded length:", len(padded))
    return [int(padded[i:i+8], 2) for i in range(0, len(padded), 8)]