# TODO: 
# - maybe make a separate notif queue?
# - look for a specific MAC address, in case you have multiple and want to use each for a specifc purpose
# CONSIDER:
# - class-ize the communication
# CHECK: 
# - don't build up notification requests before connect


import sys, time, asyncio, threading, traceback, io

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
import PIL.Image
import PIL.ImageDraw
import PIL.ImageFont
import PIL.ImageChops
from flask import Flask, request, jsonify


########################### constants, variables

# Commands
RetractPaper           = 0xA0  # Data: Number of steps to go back
FeedPaper              = 0xA1  # Data: Number of steps to go forward
DrawBitmap             = 0xA2  # Data: Line to draw. 0 bit -> don't draw pixel, 1 bit -> draw pixel
GetDevState            = 0xA3  # Data: 0
ControlLattice         = 0xA6  # Data: Eleven bytes, all constants. One set used before printing, one after.
GetDevInfo             = 0xA8  # Data: 0
OtherFeedPaper         = 0xBD  # Data: one byte, set to a device-specific "Speed" value before printing and to 0x19 before feeding blank paper
DrawingMode            = 0xBE  # Data: 1 for Text, 0 for Images
SetEnergy              = 0xAF  # Data: 1 - 0xFFFF
SetQuality             = 0xA4  # Data: 0x31 - 0x35. APK always sets 0x33 for GB01

PrintLattice           = [ 0xAA, 0x55, 0x17, 0x38, 0x44, 0x5F, 0x5F, 0x5F, 0x44, 0x38, 0x2C ]
FinishLattice          = [ 0xAA, 0x55, 0x17, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x17 ]
#XOff                  = ( 0x51, 0x78, 0xAE, 0x01, 0x01, 0x00, 0x10, 0x70, 0xFF )
#XOn                   = ( 0x51, 0x78, 0xAE, 0x01, 0x01, 0x00, 0x00, 0x00, 0xFF )

PrinterWidth           = 384
#ImgPrintSpeed         = [ 0x23 ]
#BlankSpeed            = [ 0x19 ]

ImgPrintSpeed          = [ 0x19 ]
BlankSpeed             = [ 0x05 ]

packet_length           = 220      # not sure what the real limit is, should probably find out

PrinterCharacteristic  = "0000AE01-0000-1000-8000-00805F9B34FB"
NotifyCharacteristic   = "0000AE02-0000-1000-8000-00805F9B34FB"

#specific_macs          = ()
accepted_printer_names = ( 'MX06', 'GB01' )   # note: probably a handful more will work with this code

webport                = 8081
bluetooth_on           = False
device                 = None
awaiting_status        = False

status                 = {}
last_communication     = time.time()
command_queue          = []
image_queue            = []
text_queue             = []


########################### helpers

def trim_image(im):
    bg = PIL.Image.new(im.mode, im.size, (255,255,255))
    diff = PIL.ImageChops.difference(im, bg)
    diff = PIL.ImageChops.add(diff, diff, 2.0)
    bbox = diff.getbbox()
    if bbox:
        return im.crop((bbox[0],bbox[1],bbox[2],bbox[3]+10)) # don't cut off the end of the image


def image_strips(im):
    stripheight = 25
    ret = []
    w,h = im.size
    for yoff in range(0,h-stripheight,stripheight):
        ret.append( im.crop((0, yoff, w, yoff+stripheight)) )
    return ret


def generate_text_image(text, font_name="FreeSans.ttf", font_size=30):
    ' Copied from the mentioned code. There is likely a better way to do nice wrapping. '
    def get_wrapped_text(text: str, font: PIL.ImageFont.ImageFont, line_length: int):
        if font.getlength(text) <= line_length:
            return text
        lines = ['']
        for word in text.split():
            line = f'{lines[-1]} {word}'.strip()
            if font.getlength(line) <= line_length:
                lines[-1] = line
            else:
                lines.append(word)
        return '\n'.join(lines)

    img = PIL.Image.new('RGB', (PrinterWidth, 5000), color = (255, 255, 255))
    font = PIL.ImageFont.truetype(font_name, font_size)
    
    d = PIL.ImageDraw.Draw(img)
    lines = []
    for line in text.splitlines():
        lines.append( get_wrapped_text(line, font, PrinterWidth) )
    lines = "\n".join(lines)
    d.text((0,0), lines, fill=(0,0,0), font=font)
    return trim_image(img)


def ensure_pilim(pil_or_bytes):
    if type(pil_or_bytes) == PIL.Image.Image: 
        return pil_or_bytes
    else:
        pil_or_bytes = io.BytesIO(pil_or_bytes)
        image = PIL.Image.open(pil_or_bytes)
        # deal with possible transparency by pasting it on white (TODO: put this on 'is there an alpha channel' condition)
        try:
            new_image = PIL.Image.new("RGBA", image.size, "WHITE")
            new_image.paste(image, (0, 0), image)    
            image = new_image
        except:
            pass # ?    
        return image


########################### bluetooth and printer related

# CRC8 table extracted from APK, pretty standard though
def crc8(data):
    crc8_table = [
        0x00, 0x07, 0x0e, 0x09, 0x1c, 0x1b, 0x12, 0x15, 0x38, 0x3f, 0x36, 0x31,
        0x24, 0x23, 0x2a, 0x2d, 0x70, 0x77, 0x7e, 0x79, 0x6c, 0x6b, 0x62, 0x65,
        0x48, 0x4f, 0x46, 0x41, 0x54, 0x53, 0x5a, 0x5d, 0xe0, 0xe7, 0xee, 0xe9,
        0xfc, 0xfb, 0xf2, 0xf5, 0xd8, 0xdf, 0xd6, 0xd1, 0xc4, 0xc3, 0xca, 0xcd,
        0x90, 0x97, 0x9e, 0x99, 0x8c, 0x8b, 0x82, 0x85, 0xa8, 0xaf, 0xa6, 0xa1,
        0xb4, 0xb3, 0xba, 0xbd, 0xc7, 0xc0, 0xc9, 0xce, 0xdb, 0xdc, 0xd5, 0xd2,
        0xff, 0xf8, 0xf1, 0xf6, 0xe3, 0xe4, 0xed, 0xea, 0xb7, 0xb0, 0xb9, 0xbe,
        0xab, 0xac, 0xa5, 0xa2, 0x8f, 0x88, 0x81, 0x86, 0x93, 0x94, 0x9d, 0x9a,
        0x27, 0x20, 0x29, 0x2e, 0x3b, 0x3c, 0x35, 0x32, 0x1f, 0x18, 0x11, 0x16,
        0x03, 0x04, 0x0d, 0x0a, 0x57, 0x50, 0x59, 0x5e, 0x4b, 0x4c, 0x45, 0x42,
        0x6f, 0x68, 0x61, 0x66, 0x73, 0x74, 0x7d, 0x7a, 0x89, 0x8e, 0x87, 0x80,
        0x95, 0x92, 0x9b, 0x9c, 0xb1, 0xb6, 0xbf, 0xb8, 0xad, 0xaa, 0xa3, 0xa4,
        0xf9, 0xfe, 0xf7, 0xf0, 0xe5, 0xe2, 0xeb, 0xec, 0xc1, 0xc6, 0xcf, 0xc8,
        0xdd, 0xda, 0xd3, 0xd4, 0x69, 0x6e, 0x67, 0x60, 0x75, 0x72, 0x7b, 0x7c,
        0x51, 0x56, 0x5f, 0x58, 0x4d, 0x4a, 0x43, 0x44, 0x19, 0x1e, 0x17, 0x10,
        0x05, 0x02, 0x0b, 0x0c, 0x21, 0x26, 0x2f, 0x28, 0x3d, 0x3a, 0x33, 0x34,
        0x4e, 0x49, 0x40, 0x47, 0x52, 0x55, 0x5c, 0x5b, 0x76, 0x71, 0x78, 0x7f,
        0x6a, 0x6d, 0x64, 0x63, 0x3e, 0x39, 0x30, 0x37, 0x22, 0x25, 0x2c, 0x2b,
        0x06, 0x01, 0x08, 0x0f, 0x1a, 0x1d, 0x14, 0x13, 0xae, 0xa9, 0xa0, 0xa7,
        0xb2, 0xb5, 0xbc, 0xbb, 0x96, 0x91, 0x98, 0x9f, 0x8a, 0x8d, 0x84, 0x83,
        0xde, 0xd9, 0xd0, 0xd7, 0xc2, 0xc5, 0xcc, 0xcb, 0xe6, 0xe1, 0xe8, 0xef,
        0xfa, 0xfd, 0xf4, 0xf3
    ]


    crc = 0
    for byte in data:
        crc = crc8_table[(crc ^ byte) & 0xFF]
    return crc & 0xFF


def format_message(command, data):
    # General message format:  
    # Magic number: 2 bytes 0x51, 0x78
    # Command: 1 byte
    # 0x00
    # Data length: 1 byte
    # 0x00
    # Data: Data Length bytes
    # CRC8 of Data: 1 byte
    # 0xFF
    data = [ 0x51, 0x78 ] + [command] + [0x00] + [len(data)] + [0x00] + data + [crc8(data)] + [0xFF]
    return bytes(data)


async def request_printer_status():
    ''' periodically queue statues request to the printer (also keeping it awake?), 
        This will be run alongside  connect_catprinter_and_handle_queues

        These seem to not be sent out (so no notifs received either) while printing, 
        so either fix that, or pause queueing these while sending a print.
    '''
    global command_queue
    while 1:
        # CONSIDER: ensure a limit of these on the queue, to avoid an initial stampede when you turn it on later
        command_queue.append( format_message(GetDevState, [0x00]) + format_message(ControlLattice, FinishLattice) )
        #if device is None:
        await asyncio.sleep(4)
        #else:
        #    await asyncio.sleep(1)


def catprinter_notification_handler(sender, data):
    " 'got a notification from the printer' callback: parse, and update some globals "
    global status, awaiting_status, last_communication
    #print("NOTIF: {0}: [ {1} ]".format(sender, " ".join("{:02X}".format(x) for x in data)))
    # XOff = ( 0x51, 0x78, 0xAE, 0x01, 0x01, 0x00, 0x10, 0x70, 0xFF )
    # XOn = ( 0x51, 0x78, 0xAE, 0x01, 0x01, 0x00, 0x00, 0x00, 0xFF )
    
    # The code I took this from tests of xon and xoff, but those never seem to be received
    if data[2] == GetDevState:
        awaiting_status = False
        #print("printer status byte: {:08b}".format(data[6]))
        status = {
            # different models seem to use different subets. cover_open doesn't work on my MX06, other code had a note that low battery is the only one GB01 uses.
            "no_paper":    True if data[6] & 0b00000001 else False,
            "cover_open":  True if data[6] & 0b00000010 else False, # doesn't seem to work on all models?
            "over_temp":   True if data[6] & 0b00000100 else False,
            "battery_low": True if data[6] & 0b00001000 else False,
            "printing":    True if data[6] & 0b10000000 else False, # I'm guessing this one.  Who knows, might be a xoff thing?
        }
        if data[6] & 0b01110000: # bits we don't know yet
            print('unknown bits: ', data[6])
    last_communication = time.time()


def detect_catprinter(detected, advertisement_data):
    global device
    # This isn't necessarily a great way to detect the printer.
    # It's the way the app does it, but the app has an actual UI where you can pick your device from a list.
    # Ideally this function would filter for known characteristics, but I don't know how hard that would be or what
    # kinds of problems it could cause. For now, I just want to get my printer working.
    if detected.name in accepted_printer_names:
        device = detected


async def connect_catprinter_and_handle_queues():
    ' infinitely loop "scan for printer and connect to the first that is found; while connected, handle queues" '
    global device, command_queue, text_queue, awaiting_status, last_communication, bluetooth_on

    while 1:
        #print('while loop')
        try:

            # Scan for BLE devices (up to 5 seconds each time), see if we find one called MX06
            print("scan for printer")
            scanner = BleakScanner()
            scanner.register_detection_callback(detect_catprinter)
            await scanner.start()
            bluetooth_on = True
            for x in range(50): # 
                await asyncio.sleep(0.1)
                if device:
                    break
            await scanner.stop()
            if not device:
                raise BleakError(f"No device named %s could be found."%(' or '.join(accepted_printer_names)))

            last_communication = time.time()
            # okay, we have a device to contact, do so:
            async with BleakClient(device) as client:
                #print('<client>')
                try:
                    # Set up callback to handle messages from the printer
                    await client.start_notify(NotifyCharacteristic, catprinter_notification_handler)

                    while 1:
                        try:
                            # we want to ensure a steady stream of status notification within the scope of a connection
                            # (also if we care to listen to xoff and such)
                            #print( "waiting for notif in connection" )
                            awaiting_status = True
                            await client.write_gatt_char(PrinterCharacteristic, format_message(GetDevState, [0x00]) + format_message(ControlLattice, FinishLattice))
                            while awaiting_status: # maybe timeout, otherwise this may be a hang-forver.
                                ago_sec = time.time() - last_communication
                                #print(ago_sec)
                                if ago_sec > 10:
                                    print("seem to have lost connection")
                                    device = None
                                    awaiting_status = False
                                    break
                                await asyncio.sleep(0.01)
                            if device is None:
                                break
                            #print( " got it" )

                            if len(command_queue) > 0: 
                                # there is an argument to make this a a the while will flush everything we queued before we pick up new things to print 
                                # but also before another (forced) status update, so there is an argument for flushing only so much of what we have left
                                data = command_queue.pop(0)
                                while(data):
                                    #print("sending data to printer: %r"%data)
                                    #print( "sending %d bytes to printer"%len(data) )
                                    # Cut the command stream up into pieces small enough for the printer to handle
                                    await client.write_gatt_char(PrinterCharacteristic, bytes(data[:packet_length]))
                                    data = data[packet_length:]
                                    await asyncio.sleep(0.0001)
                                    last_communication = time.time()
                                #await asyncio.sleep(0.01)
                                continue # ensure that notification is fetched between each command - we could refine this.

                            if len(text_queue) > 0:
                                print( "Taking text off queue to print" )
                                text, font_size = text_queue.pop(0)
                                if text is not None:
                                    print('text:',text)
                                    command_queue.append( image_to_drawcommands( None, feed_amount=-50) )
                                    command_queue.append( image_to_drawcommands( generate_text_image(text, font_size=font_size), feed_amount=0, energy=17000 ) )
                                    command_queue.append( image_to_drawcommands( None, feed_amount=60) )

                            elif len(image_queue) > 0:
                                print( "Taking image off queue to print" )
                                image = image_queue.pop(0)
                                if image is not None:
                                    command_queue.append( image_to_drawcommands( None, feed_amount=-50) )
                                    command_queue.append( image_to_drawcommands( image ) )
                                    #strips = image_strips( ensure_pilim(image) )
                                    #print(strips)
                                    #for stripim in reversed( strips ):
                                    #    command_queue.append( image_to_drawcommands( stripim ) )
                                    command_queue.append( image_to_drawcommands( None, feed_amount=60) )


                            await asyncio.sleep(0.10)

                        except IndexError as ie:
                            print(ie)
                            await asyncio.sleep(0)                    
                except:
                    traceback.print_exc(file=sys.stdout)
                #print('</client>')


        except Exception as e:
            print('EEE', str(e) )

            if 'Is Bluetooth turned on' in str(e)  or "No Bluetooth adapters" in str(e): # BleakError
                # TODO: set "bluetooth missing or disabled" in status? 
                print("Bluetooth missing or disabled")
                bluetooth_on = False
                await asyncio.sleep(2) # allow you to plug one on / turn it on

            elif 'No device named' in str(e): # BleakError
                print( 'ND',str(e) )
            elif 'Not connected' in str(e):   # BleakError
                print( 'NC',str(e) )
                #device = None # TODO: check that that doesn't break anything
            else:
                traceback.print_exc(file=sys.stdout)


def image_to_drawcommands(pil_or_bytes, feed_amount=0, energy=0x7EE0):
    " Takes a PIL image, or a bytestring PIL can open -- or None, to only feed "
    cmdqueue = []
    # Ask the printer how it's doing
    cmdqueue += format_message(GetDevState, [0x00])
    # Set quality to standard
    cmdqueue += format_message(SetQuality,  [0x33])
    # start and/or set up the lattice, whatever that is
    cmdqueue += format_message(ControlLattice, PrintLattice)
    # Set energy used to a moderate level
    cmdqueue += format_message(SetEnergy,   [energy.to_bytes(2, 'little')[0], energy.to_bytes(2, 'little')[1]])
    # Set mode to image mode
    cmdqueue += format_message(DrawingMode, [0])
    # not entirely sure what this does
    cmdqueue += format_message(OtherFeedPaper, ImgPrintSpeed)

    if pil_or_bytes: # if none this is used to send the wrapping)
        # if not PIL image, assume it's bytes that PIL can open
        pil_image = ensure_pilim( pil_or_bytes )

        # If wider: resize (TODO: rotate)
        if pil_image.width > PrinterWidth:
            # image is wider than printer resolution; scale it down proportionately
            height = int(pil_image.height * (PrinterWidth / pil_image.width))
            pil_image = pil_image.resize((PrinterWidth, height))

        # convert image to black-and-white 1bpp color format
        pil_image = pil_image.convert("1")

        if pil_image.width < PrinterWidth:
            # image is narrower than printer resolution; pad it out with white pixels
            padded_image = PIL.Image.new("1", (PrinterWidth, pil_image.height), 1)
            padded_image.paste(pil_image)
            pil_image = padded_image

        #print it so it looks right when spewing out of the mouth
        pil_image = pil_image.rotate(180) 

        for y in range(0, pil_image.height):
            bmp = []
            bit = 0
            # pack image data into 8 pixels per byte
            for x in range(0, pil_image.width):
                if bit % 8 == 0:
                    bmp += [0x00]
                bmp[int(bit / 8)] >>= 1
                if not pil_image.getpixel((x, y)):
                    bmp[int(bit / 8)] |= 0x80
                else:
                    bmp[int(bit / 8)] |= 0
                bit += 1

            cmdqueue += format_message(DrawBitmap, bmp)

    # Feed some extra paper after the image
    cmdqueue += format_message(OtherFeedPaper, BlankSpeed)
    if feed_amount > 0:
        cmdqueue += format_message(FeedPaper,    [feed_amount.to_bytes(2, 'little')[0], feed_amount.to_bytes(2, 'little')[1]])
    else:
        feed_amount = abs(feed_amount)
        cmdqueue += format_message(RetractPaper, [feed_amount.to_bytes(2, 'little')[0], feed_amount.to_bytes(2, 'little')[1]])

    # iPrint sends another GetDevState request at this point, but we're not staying long enough for an answer

    # finish the lattice, whatever that means
    cmdqueue += format_message(ControlLattice, FinishLattice)

    return cmdqueue



########################### webapp part

app = Flask(__name__)

@app.route("/status",      methods=['GET', 'POST'])
def app_status():
    ' serve out current status '
    st = {
        "bluetooth_on":  bluetooth_on,
        "printer_found": (device is not None),
    }
    st.update(status)
    if device is not None:
        st["printer_address"] = device.address
        st["printer_name"] = device.name
    st["queue_len_img"] = len(image_queue)
    st["queue_len_txt"] = len(text_queue)
    st["queue_len_cmd"] = len(command_queue)
    st["lastcomm_agosec"] = round( time.time() - last_communication, 1)
    print(st) # maybe only if interesting?
    return jsonify(st)


@app.route("/print-text",  methods=['GET', 'POST'])
def print_text():
    ' take text (and size), queue for the printer code to pick up '
    global text_queue
    text      = request.form.get('text')
    font_size = int(request.form.get('fontsize', '30'))
    print(request.args)
    print('text: %r'%text)
    print('fontsize: %r'%font_size)
    if text is not None:
        text_queue.append( (text, font_size) )
        return "Sent to printer queue"
    else:
        return "no text  :("


@app.route("/print-image", methods=['GET', 'POST'])
def print_image():
    ' take image, queue for the printer code to pick up '
    global command_queue
    #print(request.files)

    imagebytes = request.files.get('imagefile').stream.read()
    #print('image: %r'%imagebytes)
    if imagebytes is not None:
        image_queue.append( imagebytes )
        return "Sent to printer queue"
    else:
        return "no image :("


@app.route('/')
def catch_all():
    ' index page, shown for all paths except the specific API paths '
    return """<!DOCTYPE html>
<html>
 <head>
    <title>Cat printer</title>
    <style>
    body    { font-family:Arial; }
    th      { text-align: right; min-width: 6em; }
    .button { background-color:#0002; border: 1px outset #ccc; display: inline-block; padding: 6px 12px; cursor: pointer; font-weight:bold; font-size: medium; }
    </style>
 </head>
 <body> 
   <div style="max-width:60em; margin:1em auto; background-color:#ddd; border-radius:1em"><div style="padding:2em">
   <table style="width:80%"><tr>
     <td style="width:25%" id="s1"></td>
     <td style="width:25%" id="s2"></td>
     <td style="width:25%" id="s3"></td>
     <td style="width:25%" id="s4"></td>
   </tr></table>
  </div></div>

   <div style="max-width:60em; margin:1em auto; background-color:#ddd; border-radius:1em"><div style="padding:2em">
   <form action="print-text">
    <textarea style="padding:1em; width:80%; height:6em; font-size:150%" name="text" id="text">Test text</textarea>
    <table><tr><th>Font size</th><td><input id="f" type="range" min="20" max="120" value="40"></td><td><span id="fv"></span></td></tr></table>
    <br/><button class="button" id="printtext">Print text</button
   </form>
  </div></div>
 
  <div style="max-width:60em; margin:1em auto; background-color:#ddd; border-radius:1em"><div style="padding:2em">
    <input type="file" id="inp" style="display:none" name="file"/><label for="inp"><span style="margin:0em 0em 1em" class="button">Choose client-side image file</span></label><br/>
    <canvas id="canvas" style="max-width:20em; border:4px dotted black"></canvas><br/>
    <table style="padding:1em 0em">
        <tr><th>Zoom</th><td><input id="z" type="range" min="0" max="800" value="0"></td><td><span id="zv"></span>%</td></tr>
        <tr><th>Brightness</th><td><input id="b" type="range" min="80" max="250" value="115"></td><td><span id="bv"></span>%</td></tr>
        <tr><th>Contrast</th><td><input id="c" type="range" min="70" max="250" value="135"></td><td><span id="cv"></span>%</td></tr>
    </table>
    <button class="button" id="printimage">Print image</button>
  </div></div>


<script>
let font_size=30;

let loaded_image;
let filtertext = 'grayscale()';
let zoom       = 0;
let brightness = 100;
let contrast   = 100;
let canvas = document.getElementById('canvas');
let canvas_ctx = canvas.getContext('2d');


function reset_image_values() { /* if you load a new image, start with the default settings */
    zoom       = 0;
    brightness = 115;
    contrast   = 135;
}

function reset_text_values() {  /* sort of pointless, but for consistency */
    font_size = 30;
}

function update_sliders() {     /* set sliders from variables. Mostly to avoid page-load inconsistencies, also used (after the above resets) after image load*/

    document.getElementById('f').value = font_size;
    document.getElementById('z').value = zoom;
    document.getElementById('b').value = brightness;
    document.getElementById('c').value = contrast;
}

function update_from_form() { /* We call this after an interaction to set the new values into the page (show what the values are) */
  document.querySelector("#fv").innerText = font_size;
  document.querySelector("#zv").innerText = Math.round(0.1*zoom); // /1000 for fraction, *100 for percent
  document.querySelector("#bv").innerText = brightness;
  document.querySelector("#cv").innerText = contrast;
  filtertext = "grayscale() brightness("+brightness+"%) contrast("+contrast+"%)";
  console.log('update_from_form:', filtertext, '; zoom:', zoom)
}


function update_status() { /* call HTTP endpoint, show useful status things on our page */
    fetch("./status")
        .then( function (result) { return result.json() } )
        .then( function (ob)   { 
            var s1 = '', s2 = '', s3 = '', s4 = '';
            if (!ob.bluetooth_on) {
                s1 = '<b>bluetooth hardware missing or disabled?</b>';
            } else if (ob.printer_found && ob.lastcomm_agosec < 3) {
                s1 = '<b>'+ob.printer_name+' connected</b>';
                if (ob.lastcomm_agosec>5)                   s1 = '<span style="color:orange">'+s1+'</span>';
                if (ob.battery_low)                         s2 += ' <span>battery low</span>'; 
                else if (ob.over_temp)                      s2 += ' <span>over temp</span>'; 
                else if (ob.cover_open || ob.no_paper)      s2 += ' <span>cover open or no paper</span>'; 
                if (ob.printing)                            s3 += " <b>...printing...</b>";
            } else {
                s1 = '<em>...scanning...<br/><small style="color:#999">('+ob.lastcomm_agosec+' sec, can take 20)</small></em>';
            }
            //console.log(s1, s2, s3, s4);
            document.getElementById('s1').innerHTML = s1;
            document.getElementById('s2').innerHTML = s2;
            document.getElementById('s3').innerHTML = s3;
            document.getElementById('s4').innerHTML = s4;
        } )
        .catch(function(error) { // for now assume this is a networkerror because you've stopped that server
            document.getElementById('s4').innerHTML = '<span style="color:red">'+error+'</span>'; //TODO: make that safer
            console.log(error);
        });
}


function img_load() { /* callback when the input-file is set: consider (makes the browser try to see it), reset and propagate filter values, show */ 
  console.log('img_load', this);
  loaded_image = this;
  reset_image_values();
  update_sliders();
  draw_loaded_image();
}


function draw_loaded_image() { /* mostly just canvas drawImage, but  */ 
  if (loaded_image == undefined) {
    console.log('no image loaded yet')
  } else {
    console.log('draw_loaded_image', loaded_image);

    // Without cropping, we could create a canvas of the same size and past the image exactly into that
    canvas.width  = loaded_image.width;
    canvas.height = loaded_image.height;
    canvas_ctx.filter = filtertext;
    //...which makes it a basic blit...
    //canvas_ctx.drawImage(loaded_image, 0,0);

    //...but we want to add a zoom, we need to do a little math
    // (A more serious setup might also reduce the canvas size, and manage aspect ratio in the process)
    var xfrac = 0.001*zoom; // separated so they could be separated later
    var yfrac = 0.001*zoom;
    var xoff = Math.round(0.5*xfrac*loaded_image.width);
    var yoff = Math.round(0.5*xfrac*loaded_image.height);
    canvas_ctx.drawImage(
        loaded_image,
        xoff, yoff, loaded_image.width-2*xoff, loaded_image.height-2*yoff,  
        0, 0, loaded_image.width, loaded_image.height  /* draw to entire canvas */
    );

  }
}

function img_load_failed() {
  console.error("The provided file couldn't be loaded as an Image media");
  // maybe add another table cell to say this?
}



/* register handlers to pick up slider bar changes, putting the new value in global variables */
document.getElementById('f').onchange = function(e) { font_size = this.value;  update_from_form(); }
document.getElementById('z').onchange = function(e) { zoom = this.value;       update_from_form(); draw_loaded_image(); }
document.getElementById('b').onchange = function(e) { brightness = this.value; update_from_form(); draw_loaded_image(); }
document.getElementById('c').onchange = function(e) { contrast = this.value;   update_from_form(); draw_loaded_image(); }

/* register 'print text' button to call the HTTP API */
document.getElementById('printtext').onclick = function(e) {
    e.preventDefault();
    let formData = new FormData();
    formData.append("text", document.getElementById('text').value);
    formData.append("fontsize", font_size);
    fetch("./print-text", { method: "post", body: formData })
        .then( function (result) { return result.text() } )
        .then( function (text)   { console.log(text)    } );
}

/* register 'if you set an upload file, see if we can draw it as an image */
document.getElementById('inp').onchange = function(e) { 
  var img = new Image();
  img.src = URL.createObjectURL( this.files[0] );
  img.onload  = img_load;        
  img.onerror = img_load_failed; 
}

/* register 'print image' button to call the HTTP API (picks up the canvas contents) */
document.getElementById('printimage').onclick = function(e) {
    e.preventDefault();
	console.log('printimage click');
    document.querySelector("#canvas").toBlob( function(blob) {
        console.log('blob',blob);
        let formData = new FormData();
        formData.append("imagefile", blob);
        fetch("./print-image", { method: "post", body: formData })
          .then( function (result) { return result.text() } )
          .then( function (text)   { console.log(text)    } );
    });
}


// Basically onload calls:
reset_image_values();
reset_text_values();
update_sliders();
update_from_form();

setInterval(update_status, 500);
</script>
 </body>
</html>
"""


# start web server in thread
threading.Thread(target=app.run, kwargs={'port':webport, 'debug':False}).start()

# point local browser at that
import webbrowser
webbrowser.open('http://localhost:%d'%webport, new=2)

# start the bluetooth communication
async def main():
    L = await asyncio.gather(
        connect_catprinter_and_handle_queues(),
        #request_printer_status()
    )
asyncio.run( main() )
