# term/deck

<img width="1200" height="600" alt="termdeck" src="https://github.com/user-attachments/assets/e99fdaad-6ac9-47cf-ae35-fbabdb62fade" />

## Background [human-written]

I've always wanted to build my own fully-functional, portable computer, and the term/deck is my first step. I have a smartphone, but it often feels like I'm renting it from a big tech company instead of actually being mine. 

The process of designing, building, and iterating is as valuable, or perhaps even more so, than the final product itself. Even if the clankers and research and output code faster than I can read it, they can't design an item, take it out of the 3d printer, and see how it feels in the hand. 

My goal with this project was to use as much as I had on hand to put this together. That means the Raspberry Pi 3A+ became the compute. Being limited to 512MB of RAM on the machine is actually an interesting challenge on how to do what you are hoping to with as little system resources as possible. No browser? Just write a TUI for whatever functionality you need. Hence https://github.com/bkiu/txlf-tui


## Bill of Materials

### Main Components

 - Raspberry Pi 3A+ (got mine from MicroCenter for < $20)
 - PiSugar 3 Plus for power management ([amazon](https://www.amazon.com/dp/B0H51CW3Q5?ref=ppx_yo2ov_dt_b_fed_asin_title))
 - Freenove 4.3" touchscreen - the cheapest ribbon cable (DSI) display I could find ([amazon](https://www.amazon.com/dp/B0DDC4KKKF?ref=ppx_yo2ov_dt_b_fed_asin_title&th=1))
 - Xbox 360 Chatpad (found on eBay for about $9)

### Wires, connectors, etc
 - JST extension cable that I put together from JST-PH 2.0mm pitch kit
 - 6 M3 screw insets
 - 2 1K-ohm resistors
 - 8 M2.5 screws (longest was 8mm from a set)
 - Thin wire (22 AWG or smaller)
 - Heat shrink
 - Soldering Equipment
 - Printing was done on a BambuLab P1S with SUNLU PLA+ filament


### Screen Assembly Details

Mount the PiSugar board on the back of the Pi 3 as instructed, and then use a JST extension cable to connect the battery to the board. The default battery cable isn't log enough, and also the battery cable gets pinned to the back of the board and can easily break it if you're not careful, and replacing the JST extension cable is easier than the actual battery cable. I used longer 8mm M2.5 screws through the PiSugar board to attach to the screen. The size of the JST cable will prevent the screws to go in all the way, and be careful about bending the JST connector too much. 

### Printing

Please see the `printables` directory for STL files. If you want to see the original CAD files, please refer to [OnShape](https://cad.onshape.com/documents/b66f9063a1f70a8d371a8e38/v/7bcf59267be2bad0e7e6bb13/e/e1c1aeb8d9a80c28f11d5a21?renderMode=0&uiState=6af2955b3ae41204093c63e0).

Please see [FROM_CLANKERS.md](FROM_CLANKERS.md) for software and wiring.

