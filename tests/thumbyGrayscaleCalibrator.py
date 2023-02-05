# Thumby grayscale library
# https://github.com/Timendus/thumby-grayscale
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


from utime import sleep_ms, ticks_diff, ticks_ms, sleep_us
from machine import Pin, SPI, idle, mem32
import _thread
from os import stat
from math import sqrt, floor
from array import array
from thumbyButton import buttonA, buttonB, buttonU, buttonD, buttonL, buttonR
from thumbyHardware import HWID
from sys import modules

__version__ = '3.0.0'


emulator = None
try:
    import emulator
except ImportError:
    pass


class Sprite:
    @micropython.native
    def __init__(self, width, height, bitmapData, x = 0, y=0, key=-1, mirrorX=False, mirrorY=False):
        self.width = width
        self.height = height
        self.bitmapSource = bitmapData
        self.bitmapByteCount = width*(height//8)
        if(height%8):
            self.bitmapByteCount+=width
        self.frameCount = 1
        self.currentFrame = 0
        self._shaded = False
        self._usesFile = False
        if isinstance(bitmapData, (tuple, list)):
            if (len(bitmapData) != 2) or (type(bitmapData[0]) != type(bitmapData[1])):
                raise ValueError('bitmapData must be a bytearray, string, or tuple of two bytearrays or strings')
            self._shaded = True
            if isinstance(bitmapData[0], str):
                self._usesFile = True
                if stat(bitmapData[0])[6] != stat(bitmapData[1])[6]:
                    raise ValueError('Sprite files must match in size')
                self.bitmap = (bytearray(self.bitmapByteCount), bytearray(self.bitmapByteCount))
                self.files = (open(bitmapData[0],'rb'),open(bitmapData[1],'rb'))
                self.files[0].readinto(self.bitmap[0])
                self.files[1].readinto(self.bitmap[1])
                self.frameCount = stat(bitmapData[0])[6] // self.bitmapByteCount
            elif isinstance(bitmapData[0], bytearray):
                if len(bitmapData[0]) != len(bitmapData[1]):
                    raise ValueError('Sprite bitplanes must match in size')
                self.frameCount = len(bitmapData[0]) // self.bitmapByteCount
                self.bitmap = [
                    memoryview(bitmapData[0])[0:self.bitmapByteCount],
                    memoryview(bitmapData[1])[0:self.bitmapByteCount]
                ]
            else:
                raise ValueError('bitmapData must be a bytearray, string, or tuple of two bytearrays or strings')
        elif isinstance(bitmapData, str):
            self._usesFile = True
            self.bitmap = bytearray(self.bitmapByteCount)
            self.file = open(bitmapData,'rb')
            self.file.readinto(self.bitmap)
            self.frameCount = stat(bitmapData)[6] // self.bitmapByteCount
        elif isinstance(bitmapData, bytearray):
            self.bitmap = memoryview(bitmapData)[0:self.bitmapByteCount]
            self.frameCount = len(bitmapData) // self.bitmapByteCount
        else:
            raise ValueError('bitmapData must be a bytearray, string, or tuple of two bytearrays or strings')
        self.x = x
        self.y = y
        self.key = key
        self.mirrorX = mirrorX
        self.mirrorY = mirrorY

    @micropython.native
    def getFrame(self):
        return self.currentFrame

    @micropython.native
    def setFrame(self, frame):
        if(frame >= 0 and (self.currentFrame is not frame % (self.frameCount))):
            self.currentFrame = frame % (self.frameCount)
            offset=self.bitmapByteCount*self.currentFrame
            if self._shaded:
                if self._usesFile:
                    self.files[0].seek(offset)
                    self.files[1].seek(offset)
                    self.files[0].readinto(self.bitmap[0])
                    self.files[1].readinto(self.bitmap[1])
                else:
                    self.bitmap[0] = memoryview(self.bitmapSource[0])[offset:offset+self.bitmapByteCount]
                    self.bitmap[1] = memoryview(self.bitmapSource[1])[offset:offset+self.bitmapByteCount]
            else:
                if self._usesFile:
                    self.file.seek(offset)
                    self.file.readinto(self.bitmap)
                else:
                    self.bitmap = memoryview(self.bitmapSource)[offset:offset+self.bitmapByteCount]


# Display clock frequency calibrator
# Various display devices will run at naturally different clock
# frequencies. This paramater allows for varying to adjust the
# timings to match the different devices.
# This default number is taken from the clocks per row (1+1+50),
# and a value of 530kHz for the highest nominal clock frequency.
calibrator = array('I', [94])

# Thread state variables for managing the Grayscale Thread
_THREAD_STOPPED    = const(0)
_THREAD_RUNNING    = const(1)
_THREAD_STOPPING   = const(2)

# Indexes into the multipurpose state array, accessing a particular status
_ST_THREAD       = const(0)
_ST_COPY_BUFFS   = const(1)
_ST_PENDING_CMD  = const(2)
_ST_CONTRAST     = const(3)
_ST_INVERT       = const(4)

# Screen display size constants
_WIDTH = const(72)
_HEIGHT = const(40)
_BUFF_SIZE = const((_HEIGHT // 8) * _WIDTH)
_BUFF_INT_SIZE = const(_BUFF_SIZE // 4)


class Grayscale:

    # BLACK and WHITE is 0 and 1 to be compatible with the standard Thumby API
    BLACK     = 0
    WHITE     = 1
    DARKGRAY  = 2
    LIGHTGRAY = 3

    def __init__(self):
        self._spi = SPI(0, sck=Pin(18), mosi=Pin(19))
        self._dc = Pin(17)
        self._cs = Pin(16)
        self._res = Pin(20)
        self._spi.init(baudrate=100 * 1000 * 1000, polarity=0, phase=0)
        self._res.init(Pin.OUT, value=1)
        self._dc.init(Pin.OUT, value=0)
        self._cs.init(Pin.OUT, value=1)

        self.display = self # This acts as both the GraphicsClass and SSD1306
        self.pages = _HEIGHT // 8
        self.width = _WIDTH
        self.height = _HEIGHT
        self.max_x = _WIDTH - 1
        self.max_y = _HEIGHT - 1

        # Draw buffers.
        # This comprises of two full buffer lengths.
        # The first section contains black and white compatible
        # with the display buffer from the standard Thumby API,
        # and the second contains the shading to create
        # offwhite (lightgray) or offblack (darkgray).
        self.drawBuffer = bytearray(_BUFF_SIZE*2)
        # The base "buffer" matches compatibility with the std Thumby API.
        self.buffer = memoryview(self.drawBuffer)[:_BUFF_SIZE]
        # The "shading" buffer adds the grayscale
        self.shading = memoryview(self.drawBuffer)[_BUFF_SIZE:]

        self._subframes = array('O', [bytearray(_BUFF_SIZE),
            bytearray(_BUFF_SIZE), bytearray(_BUFF_SIZE)])

        # We enhance the greys by modulating the brightness.
        # 0x81,<val>        Set Bank0 brightness value to <val>
        # Use setting from thumby.cfg
        self._brightness = 127
        try:
            with open("thumby.cfg", "r") as fh:
                _, _, conf = fh.read().partition("brightness,")
                b = int(conf.split(',')[0])
                # Set to the relevant brightness level
                if b == 0: self._brightness = 1
                if b == 1: self._brightness = 28
                # Otherwise, leave it at 127
        except (OSError, ValueError):
            pass
        self._contrast = bytearray(3)
        self._contrastSrc = bytearray(3)

        # It's better to avoid using regular variables for thread sychronisation.
        # Instead, elements of an array/bytearray should be used.
        # We're using a uint32 array here, as that should hopefully further ensure
        # the atomicity of any element accesses.
        # [thread_state, buff_copy_gate, pending_cmd_gate, constrast_change, inverted]
        self._state = array('I', [_THREAD_STOPPED,0,0,0,0])

        self._pendingCmds = bytearray(8)

        self.setFont('lib/font5x7.bin', 5, 7, 1)

        self.lastUpdateEnd = 0
        self.frameRate = 0

        self._initEmuScreen()

        # Copy draw buffer from the standard library if it's been used
        if 'thumbyGraphics' in modules:
            self.buffer[:] = modules['thumbyGraphics'].display.display.buffer
        # Initialise the device to be capable of grayscale
        self.init_display()

        self.brightness(self._brightness)


    @micropython.viper
    def _initEmuScreen(self):
        if not emulator:
            return
        # Register draw buffer with emulator
        Pin(2, Pin.OUT) # Ready display handshake pin
        emulator.screen_breakpoint(ptr16(self.drawBuffer))
        self._clearEmuFunctions()

    def _clearEmuFunctions(self):
        # Disable device controller functions
        def _disabled(*arg, **kwdarg):
            pass
        self.invert = _disabled
        self.reset = _disabled
        self.poweron = _disabled
        self.poweroff = _disabled
        self.init_display = _disabled
        self.write_cmd = _disabled

    def reset(self):
        self._res(1)
        sleep_ms(1)
        self._res(0)
        sleep_ms(10)
        self._res(1)
        sleep_ms(10)

    def init_display(self):
        self.reset()
        self._cs(0)
        self._dc(0)
        # initialise as usual, except with shortest pre-charge
        # periods and highest clock frequency
        # 0xae          Display Off
        # 0x20,0x00     Set horizontal addressing mode
        # 0x40          Set display start line to 0
        # 0xa1          Set segment remap mode 1
        # 0xa8,39       Set multiplex ratio to 39 (will be modified)
        # 0xc8          Set COM output scan direction 1
        # 0xd3,0        Set display offset to 0 (will be modified)
        # 0xda,0x12     Set COM pins hardware configuration: alternative config,
        #                   disable left/right remap
        # 0xd5,0xf0     Set clk div ratio = 1, and osc freq = ~530kHz (480-590)kHz
        # 0xd9,0x11     Set pre-charge periods: phase 1 = 1 , phase 2 = 1
        # 0xdb,0x20     Set Vcomh deselect level = 0.77 x Vcc
        # 0xa4          Do not enable entire display (i.e. use GDRAM)
        # 0xa6          Normal (not inverse) display (invert is simulated)
        # 0x8d,0x14     Charge bump setting: enable charge pump during display on
        # 0xad,0x30     Select internal 30uA Iref (max Iseg=240uA) during display on
        # 0xf           Set display on
        self._spi.write(bytearray([
            0xae, 0x20,0x00, 0x40, 0xa1, 0xa8,39, 0xc8, 0xd3,0, 0xda,0x12,
            0xd5,0xf0, 0xd9,0x11, 0xdb,0x20, 0xa4, 0xa6, 0x8d,0x14, 0xad,0x30, 0xaf]))
        self._dc(1)
        # Clear the entire GDRAM
        zero32 = bytearray([0] * 32)
        for _ in range(32):
            self._spi.write(zero32)
        self._dc(0)
        # Set the GDRAM window
        # 0x21,28,99    Set column start (28) and end (99) addresses
        # 0x22,0,4      Set page start (0) and end (4) addresses0
        self._spi.write(bytearray([0x21,28,99, 0x22,0,4]))

    def enableGrayscale(self):
        if emulator:
            # Activate grayscale emulation
            emulator.screen_breakpoint(1)
            self.show()
            return

        if self._state[_ST_THREAD] == _THREAD_RUNNING:
            return

        # Start the grayscale timing thread and wait for it to initialise
        _thread.stack_size(2048)
        _thread.start_new_thread(self._display_thread, ())
        while self._state[_ST_THREAD] != _THREAD_RUNNING: idle()

    def disableGrayscale(self):
        if emulator:
            # Disable grayscale emulation
            emulator.screen_breakpoint(0)
            self.show()
            return

        if self._state[_ST_THREAD] != _THREAD_RUNNING:
            return
        self._state[_ST_THREAD] = _THREAD_STOPPING
        while self._state[_ST_THREAD] != _THREAD_STOPPED:
            idle()
        # Draw B/W view of current frame
        self.show()
        # Resume device color inversion
        if self._state[_ST_INVERT]:
            self.write_cmds(0xa6 | 1)
        # Change back to the original (unmodulated) brightness setting
        self.brightness(self._brightness)


    @micropython.native
    def write_cmd(self, cmd):
        if isinstance(cmd, list):
            cmd = bytearray(cmd)
        elif not isinstance(cmd, bytearray):
            cmd = bytearray([cmd])
        if self._state[_ST_THREAD] == _THREAD_RUNNING:
            pendingCmds = self._pendingCmds
            if len(cmd) > len(pendingCmds):
                # We can't just break up the longer list of commands automatically, as we
                # might end up separating a command and its parameter(s).
                raise ValueError('Cannot send more than %u bytes using write_cmd()' % len(pendingCmds))
            i = 0
            while i < len(cmd):
                pendingCmds[i] = cmd[i]
                i += 1
            # Fill the rest of the bytearray with display controller NOPs
            # This is probably better than having to create slice or a memoryview in the GPU thread
            while i < len(pendingCmds):
                pendingCmds[i] = 0x3e
                i += 1
            self._state[_ST_PENDING_CMD] = 1
            while self._state[_ST_PENDING_CMD]:
                idle()
        else:
            self._dc(0)
            self._spi.write(cmd)

    def poweroff(self):
        self.write_cmd(0xae)
    def poweron(self):
        self.write_cmd(0xaf)


    @micropython.viper
    def show(self):
        state = ptr32(self._state)
        if state[_ST_THREAD] == _THREAD_RUNNING:
            state[_ST_COPY_BUFFS] = 1
            while state[_ST_COPY_BUFFS] != 0:
                idle()
        elif emulator:
            mem32[0xD0000000+0x01C] = 1 << 2
        else:
            self._dc(1)
            self._spi.write(self.buffer)


    @micropython.native
    def setFPS(self, newFrameRate):
        self.frameRate = newFrameRate

    @micropython.native
    def update(self):
        self.show()
        if self.frameRate > 0:
            frameTimeMs = 1000 // self.frameRate
            lastUpdateEnd = self.lastUpdateEnd
            frameTimeRemaining = frameTimeMs - ticks_diff(ticks_ms(), lastUpdateEnd)
            while frameTimeRemaining > 1:
                buttonA.update()
                buttonB.update()
                buttonU.update()
                buttonD.update()
                buttonL.update()
                buttonR.update()
                sleep_ms(1)
                frameTimeRemaining = frameTimeMs - ticks_diff(ticks_ms(), lastUpdateEnd)
            while frameTimeRemaining > 0:
                frameTimeRemaining = frameTimeMs - ticks_diff(ticks_ms(), lastUpdateEnd)
        self.lastUpdateEnd = ticks_ms()


    @micropython.viper
    def brightness(self, c:int):
        if c < 0: c = 0
        if c > 127: c = 127
        state = ptr32(self._state)
        contrastSrc = ptr8(self._contrastSrc)

        # Prepare contrast for the different subframe layers:
        #  Low   (1): [ 1,  1,  10]
        #  Mid  (28): [20, 20, 138]
        # High (127): [46, 46, 255]
        cc = int(floor(sqrt(c<<17)))
        contrastSrc[0] = (cc*50>>12)-3
        contrastSrc[1] = contrastSrc[0]
        c3 = (cc*340>>12)-20
        contrastSrc[2] = c3 if c3 < 255 else 255

        # Apply to display, GPU, and emulator
        if state[_ST_THREAD] == _THREAD_RUNNING:
            state[_ST_CONTRAST] = 1
        else:
            # Apply the brightness directly to the display or emulator
            if emulator:
                emulator.brightness_breakpoint(c)
            else:
                self.write_cmd([0x81, c])
        setattr(self, '_brightness', c)


    # GPU (Gray Processing Unit) thread function
    @micropython.viper
    def _display_thread(self):
        # Rapidly draws 3 sub-frame layers per frame to simulate
        # grayscale in a thread which runs on core1. Every sub-frame
        # includes the fully lit white pixels, and only some sub-frames
        # includes the gray pixels to modulate the brightness of the
        # gray pixels.

        # MicroPython calls which could run directly off of memory
        # mapped flash are carefully avoided, as this can cause certain
        # calls on core0 to crash.

        # This thread uses a hardware timing trick to keep the SSD1306
        # synchronised with rapid switching between white and gray pixel
        # layers to simulate grayscale with minimal flicker or artifacts

        # The hardware timing trick works by creating an offscreen area
        # to briefly capture the row counter for long enough to be able
        # to change the frame contents and release them together. This
        # is done by changing the multiplex ratio (mux) to 56 giving 57
        # rows instead of the normal 40.
        # To match this, the display offset is set to 46, which aligns
        # the 40 row frame into position of the visible area, and also
        # leaves enough space to create a 16 row capture area offscreen.
        # The row counter is capture in this offscreen area by setting
        # multiplex ratio to 15.

        # Init: set DISPLAY_OFFSET:47
        # Timing Loop:
        #   * set MUX:15 (capture row counter)
        #   * draw sub-frame layer
        #   * wait long enough to ensure we capture the row counter.
        #   * set MUX:56 (release row counter)
        #   * wait long enoough for sub-frame layer to be drawn.

        state = ptr32(self._state)
        calib = ptr32(calibrator)
        contrastSrc = ptr8(self._contrastSrc)
        contrast = ptr8(bytearray(self._contrastSrc))
        pendingCmds = ptr8(self._pendingCmds)
        subframes = ptr32(array('L', [
            ptr8(self._subframes[0]),
            ptr8(self._subframes[1]),
            ptr8(self._subframes[2])]))
        d1 = int(0xAA55AA55)
        d2 = int(0x55AA55AA)

        # Draw and sub-frame buffers in 32bit for fast copying
        bb = ptr32(self.buffer)
        bs = ptr32(self.shading)
        b1 = ptr32(self._subframes[0])
        b2 = ptr32(self._subframes[1])
        b3 = ptr32(self._subframes[2])

        # Hardware register access
        spi0 = ptr32(0x4003c000)
        tmr = ptr32(0x40054000)
        sio = ptr32(0xd0000000)

        # Update the sub-frame layers for the first frame
        i = 0
        inv = -1 if state[_ST_INVERT] else 0
        # fast copy loop. By using using ptr32 vars we copy 3 bytes at a time.
        while i < _BUFF_INT_SIZE:
            v1 = bb[i] ^ inv
            v2 = bs[i]
            # layer1 -> white || lightGray || dither-darkGray [DIM]
            # layer2 -> white || lightGray || dither-darkGray(alt) [DIM]
            # layer3 -> white [BRIGHT]
            b1[i] = v1 | (v2 & (d1 if (i%4+i)%2 else d2))
            b2[i] = v1 | (v2 & (d2 if (i%4+i)%2 else d1))
            b3[i] = v1 & (v1 ^ v2)
            i += 1

        # Command Mode
        while (spi0[3] & 4) == 4: i = spi0[2]
        while (spi0[3] & 0x10) == 0x10: pass
        while (spi0[3] & 4) == 4: i = spi0[2]
        sio[6] = 1 << 17 # dc(0)

        # Set the display offset to allow space for the captured
        # row counter, and overflow area.
        spi0[2] = 0xd3; spi0[2] = 47

        state[_ST_THREAD] = _THREAD_RUNNING
        while state[_ST_THREAD] == _THREAD_RUNNING:
            # This is the main GPU loop. We cycle through each of the 3 display
            # framebuffers, sending the framebuffer data and various commands.
            fn = 0
            while fn < 3:
                # Calculate timings
                time_new = tmr[10]
                time_pre = time_new + 700
                time_end = time_new + 56*calib[0]

                # Park Display (capture row counter offscreen)
                spi0[2] = 0xa8; spi0[2] = 1

                # Data Mode
                while (spi0[3] & 4) == 4: i = spi0[2]
                while (spi0[3] & 0x10) == 0x10: pass
                while (spi0[3] & 4) == 4: i = spi0[2]
                sio[5] = 1 << 17 # dc(1)

                # Draw (sub-frame) Layer
                i = 0
                layer = ptr8(subframes[fn])
                while i < 360:
                    while (spi0[3] & 2) == 0: pass
                    spi0[2] = layer[i]
                    i += 1

                # Command Mode
                while (spi0[3] & 4) == 4: i = spi0[2]
                while (spi0[3] & 0x10) == 0x10: pass
                while (spi0[3] & 4) == 4: i = spi0[2]
                sio[6] = 1 << 17 # dc(0)

                # Brightness adjustment for sub-frame layer
                spi0[2] = 0x81; spi0[2] = contrast[fn]

                # Wait long enough to ensure we captured the row counter.
                while (tmr[10] - time_pre) < 0: pass

                # Brightness sent again for stability
                spi0[2] = 0x81; spi0[2] = contrast[fn]

                # Release Display
                spi0[2] = 0xa8; spi0[2] = 56

                if fn == 2:
                    # check if there's a pending frame copy required
                    # we only copy the paint framebuffers to the display framebuffers on
                    # the last frame to avoid screen-tearing artefacts
                    if state[_ST_COPY_BUFFS] != 0:
                        i = 0
                        inv = -1 if state[_ST_INVERT] else 0
                        # fast copy loop. By using using ptr32 vars we copy 3 bytes at a time.
                        while i < _BUFF_INT_SIZE:
                            v1 = bb[i] ^ inv
                            v2 = bs[i]
                            # layer1 -> white || lightGray || dither-darkGray [DIM]
                            # layer2 -> white || lightGray || dither-darkGray(alt) [DIM]
                            # layer3 -> white [BRIGHT]
                            b1[i] = v1 | (v2 & (d1 if (i%4+i)%2 else d2))
                            b2[i] = v1 | (v2 & (d2 if (i%4+i)%2 else d1))
                            b3[i] = v1 & (v1 ^ v2)
                            i += 1
                        state[_ST_COPY_BUFFS] = 0
                if fn == 2:
                    # check if there's a pending contrast/brightness value change
                    if state[_ST_CONTRAST] != 0:
                        # Copy in the new contrast adjustments
                        contrast[0] = contrastSrc[0]
                        contrast[1] = contrastSrc[1]
                        contrast[2] = contrastSrc[2]
                        state[_ST_CONTRAST] = 0
                    # check if there are pending commands
                    elif state[_ST_PENDING_CMD] != 0:
                        #spi_write(pending_cmds)
                        i = 0
                        while i < 8:
                            while (spi0[3] & 2) == 0: pass
                            spi0[2] = pendingCmds[i]
                            i += 1

                        state[_ST_PENDING_CMD] = 0

                # Wait until the row counter is between the end of the drawn
                # area and the end of the multiplex ratio range.
                while (tmr[10] - time_end) < 0: pass

                fn += 1

        # Restore Monochrome (display offset and mux row numbers)
        spi0[2] = 0xd3; spi0[2] = 0
        spi0[2] = 0xa8; spi0[2] = 39

        # mark that we've stopped
        state[_ST_THREAD] = _THREAD_STOPPED


    @micropython.viper
    def fill(self, colour:int):
        buffer = ptr32(self.buffer)
        shading = ptr32(self.shading)
        f1 = -1 if colour & 1 else 0
        f2 = -1 if colour & 2 else 0
        i = 0
        while i < _BUFF_INT_SIZE:
            buffer[i] = f1
            shading[i] = f2
            i += 1


    @micropython.viper
    def drawFilledRectangle(self, x:int, y:int, width:int, height:int, colour:int):
        if x >= _WIDTH or y >= _HEIGHT: return
        if width <= 0 or height <= 0: return
        if x < 0:
            width += x
            x = 0
        if y < 0:
            height += y
            y = 0
        x2 = x + width
        y2 = y + height
        if x2 > _WIDTH:
            x2 = _WIDTH
            width = _WIDTH - x
        if y2 > _HEIGHT:
            y2 = _HEIGHT
            height = _HEIGHT - y

        buffer = ptr8(self.buffer)
        shading = ptr8(self.shading)

        o = (y >> 3) * _WIDTH
        oe = o + x2
        o += x
        strd = _WIDTH - width

        c1 = colour & 1
        c2 = colour & 2
        v1 = 0xff if c1 else 0
        v2 = 0xff if c2 else 0

        yb = y & 7
        ybh = 8 - yb
        if height <= ybh:
            m = ((1 << height) - 1) << yb
        else:
            m = 0xff << yb
        im = 255-m
        while o < oe:
            if c1:
                buffer[o] |= m
            else:
                buffer[o] &= im
            if c2:
                shading[o] |= m
            else:
                shading[o] &= im
            o += 1
        height -= ybh
        while height >= 8:
            o += strd
            oe += _WIDTH
            while o < oe:
                buffer[o] = v1
                shading[o] = v2
                o += 1
            height -= 8
        if height > 0:
            o += strd
            oe += _WIDTH
            m = (1 << height) - 1
            im = 255-m
            while o < oe:
                if c1:
                    buffer[o] |= m
                else:
                    buffer[o] &= im
                if c2:
                    shading[o] |= m
                else:
                    shading[o] &= im
                o += 1


    def setFont(self, fontFile, width, height, space):
        sz = stat(fontFile)[6]
        self.font_bmap = bytearray(sz)
        with open(fontFile, 'rb') as fh:
            fh.readinto(self.font_bmap)
        self.font_width = width
        self.font_height = height
        self.font_space = space
        self.font_glyphcnt = sz // width


    @micropython.viper
    def drawText(self, stringToPrint, x:int, y:int, colour:int):
        buffer = ptr8(self.buffer)
        shading = ptr8(self.shading)
        font_bmap = ptr8(self.font_bmap)
        font_width = int(self.font_width)
        font_space = int(self.font_space)
        font_glyphcnt = int(self.font_glyphcnt)
        sm1o = 0xff if colour & 1 else 0
        sm1a = 255 - sm1o
        sm2o = 0xff if colour & 2 else 0
        sm2a = 255 - sm2o
        ou = (y >> 3) * _WIDTH + x
        ol = ou + _WIDTH
        shu = y & 7
        shl = 8 - shu
        for c in memoryview(stringToPrint):
            if isinstance(c, str):
                co = int(ord(c)) - 0x20
            else:
                co = int(c) - 0x20
            if co < font_glyphcnt:
                gi = co * font_width
                gx = 0
                while gx < font_width:
                    if 0 <= x < _WIDTH:
                        gb = font_bmap[gi + gx]
                        gbu = gb << shu
                        gbl = gb >> shl
                        if 0 <= ou < _BUFF_SIZE:
                            # paint upper byte
                            buffer[ou] = (buffer[ou] | (gbu & sm1o)) & 255-(gbu & sm1a)
                            shading[ou] = (shading[ou] | (gbu & sm2o)) & 255-(gbu & sm2a)
                        if (shl != 8) and (0 <= ol < _BUFF_SIZE):
                            # paint lower byte
                            buffer[ol] = (buffer[ol] | (gbl & sm1o)) & 255-(gbl & sm1a)
                            shading[ol] = (shading[ol] | (gbl & sm2o)) & 255-(gbl & sm2a)
                    ou += 1
                    ol += 1
                    x += 1
                    gx += 1
            ou += font_space
            ol += font_space
            x += font_space


    def calibrate(self):
        from thumbyButton import inputJustPressed
        presets = [96, 122]
        rec = self.drawFilledRectangle
        tex = self.drawText
        def info(*m):
            self.disableGrayscale()
            self.fill(0)
            for i, l in enumerate(m):
                tex(l, 0, i*8, 1)
            self.update()
            while not inputJustPressed(): idle()
            self.enableGrayscale()
        s = [0, 0]
        def sample(title, param, offset):
            rec(0, 0, 72, 40, 1)
            rec(2, 0, 68, 30, 3)
            rec(8, 0, 56, 20, 2)
            rec(16, 0, 40, 10, 0)
            tex(title, 17, 1, 3)
            tex(param, offset, 12, 1)
            tex("GRAYSCALE", 10, 22, 2)
            tex("CALIBRATION", 4, 32, 0)
            self.update()
            if s[0]%6<3 or buttonL.pressed():
                tex("<", 16, 12, 1)
            if s[0]%6>=3 or buttonR.pressed():
                tex(">", 52, 12, 1)
            s[0] += 1
            s[1] = s[1] + 1 if buttonL.pressed() or buttonR.pressed() else 0
            return (-1 if ((buttonL.pressed() and s[1]>3) or buttonL.justPressed())
                else 1 if ((buttonR.pressed() and s[1]>3) or buttonR.justPressed())
                else 0)
        origFPS = self.frameRate
        self.setFPS(5)
        self.setFont("/lib/font5x7.bin", 5, 7, 1)

        info("", "CALIBRATE", "", "GRAYSCALE...")
        info("Pick clearer", "  image with", "   <-  ->", "then press A", "         ...")
        p = 0
        while not buttonA.justPressed():
            p = (p + sample("Preset:", chr(p+65), 34)) % len(presets)
            calibrator[0] = presets[p]

        info(" Fine-tune", " image for", "less flicker", "then press A", "         ...")
        while not buttonA.justPressed():
            calibrator[0] = min(200, max(1, calibrator[0] +
                sample(" Tune:", str(calibrator[0]), 28)))

        self.setFPS(origFPS)
        self.fill(2)
        for i, l in enumerate([
            " CALIBRATED!", "", " Press any", "  button to", "     save..."]):
            tex(l, 0, i*8, 1)
        self.update()
        while not inputJustPressed(): idle()
        with open("thumbyGS.cfg", "w") as fh:
            fh.write(f"timing,{str(calibrator[0])}")

display = Grayscale()
display.calibrate()