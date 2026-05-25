import React, { useState } from 'react';
import { 
  Search, Filter, ChevronLeft, ChevronRight, Maximize2, 
  Play, Pause, SkipForward, SkipBack, MessageSquare, 
  AlertTriangle, Copy, Sun, Book, User, DownloadCloud, 
  Sparkles, RefreshCw, MousePointer2, Settings,
  RotateCcw, SplitSquareHorizontal
} from 'lucide-react';

const App = () => {
  const [activeTab, setActiveTab] = useState('Action');

  return (
    <div className="flex flex-col h-screen bg-[#F4F5F8] text-gray-800 font-sans text-sm">
      {/* Top Navigation Bar */}
      <header className="flex items-center justify-between px-4 py-2 border-b border-gray-200 bg-white shrink-0 shadow-sm z-10">
        <div className="flex items-center gap-6">
          <div className="flex items-center gap-2">
            <div className="w-6 h-6 bg-indigo-600 rounded flex items-center justify-center text-white font-bold text-xs">
              A
            </div>
            <span className="font-semibold text-gray-900">AgentRun Studio</span>
            <span className="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-600 text-[10px] font-medium border border-indigo-100">
              Beta
            </span>
          </div>
          <div className="text-gray-400 text-xs flex items-center gap-2">
            <span className="hover:text-gray-700 cursor-pointer">Projects</span>
            <span>/</span>
            <span className="hover:text-gray-700 cursor-pointer">Hotel Booking</span>
            <span>/</span>
            <span className="text-gray-900 font-medium">Run 00123</span>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <button className="flex items-center gap-2 px-3 py-1.5 text-xs font-medium border border-gray-200 rounded-md hover:bg-gray-50 text-gray-700">
            <DownloadCloud size={14} />
            Import Dataset
          </button>
          <div className="h-4 w-px bg-gray-300"></div>
          <button className="p-1.5 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded">
            <Sun size={16} />
          </button>
          <button className="p-1.5 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded">
            <Book size={16} />
          </button>
          <button className="w-7 h-7 rounded-full bg-indigo-100 text-indigo-700 flex items-center justify-center font-medium">
            U
          </button>
        </div>
      </header>

      {/* Main Workspace */}
      <div className="flex flex-1 overflow-hidden p-4 gap-4">
        
        {/* Left Sidebar - Runs List */}
        <aside className="w-72 bg-white rounded-xl border border-gray-200 flex flex-col shrink-0 shadow-sm overflow-hidden">
          <div className="p-3 border-b border-gray-200">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-wider">Sessions</h2>
              <button className="flex items-center gap-1 text-xs px-2 py-1 border border-gray-200 rounded hover:bg-gray-100 bg-white">
                <span className="text-lg leading-none mb-0.5">+</span> New Session
              </button>
            </div>
            <div className="relative mb-3">
              <Search size={14} className="absolute left-2.5 top-2 text-gray-400" />
              <input 
                type="text" 
                placeholder="Search runs..." 
                className="w-full pl-8 pr-8 py-1.5 text-sm border border-gray-200 rounded-md bg-white focus:outline-none focus:ring-1 focus:ring-indigo-500 focus:border-indigo-500"
              />
              <Filter size={14} className="absolute right-2.5 top-2 text-gray-400 cursor-pointer hover:text-gray-600" />
            </div>
            <div className="flex gap-2 text-xs">
              <button className="text-gray-600 font-medium hover:text-gray-900">All <span className="text-gray-400 ml-1">25</span></button>
              <button className="text-red-600 font-medium bg-red-50 px-2 rounded-full">Failed <span className="ml-1">8</span></button>
              <button className="text-gray-600 hover:text-gray-900">Success <span className="text-gray-400 ml-1">12</span></button>
              <button className="text-gray-600 hover:text-gray-900">Review <span className="text-gray-400 ml-1">5</span></button>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto p-2 space-y-2">
            {/* Active Card */}
            <div className="p-3 border border-red-200 bg-red-50/30 rounded-lg shadow-sm relative overflow-hidden cursor-pointer">
              <div className="absolute left-0 top-0 bottom-0 w-1 bg-red-500"></div>
              <div className="flex justify-between items-start mb-1">
                <span className="font-semibold text-gray-900">Run 00123</span>
                <span className="text-[10px] font-medium px-1.5 py-0.5 bg-red-100 text-red-700 rounded">Failed</span>
              </div>
              <p className="text-xs text-gray-600 mb-3 truncate">Find a refundable hotel under $200</p>
              <div className="flex justify-between items-center text-[10px] text-gray-500">
                <span>May 18, 2025</span>
                <div className="flex gap-3">
                  <span>14 steps</span>
                  <span className="flex items-center gap-1"><MessageSquare size={10} /> 3</span>
                  <Sparkles size={10} className="text-orange-400" />
                </div>
              </div>
            </div>

            {/* Other Cards */}
            <RunCard id="00122" status="Success" title="Find a refundable hotel under $200" date="May 18, 2025" steps="12" msgs="2" />
            <RunCard id="00121" status="Needs Review" title="Book flight from SFO to NRT" date="May 18, 2025" steps="18" msgs="5" />
            <RunCard id="00120" status="Failed" title="Search noise-canceling headphones" date="May 17, 2025" steps="11" msgs="1" />
            <RunCard id="00119" status="Success" title="Compare laptop prices on Amazon" date="May 17, 2025" steps="9" msgs="0" />
            <RunCard id="00118" status="Failed" title="Fill form and submit contact request" date="May 17, 2025" steps="15" msgs="4" />
            
            <button className="w-full py-2 text-xs text-gray-500 hover:text-gray-700">Load more...</button>
          </div>
        </aside>

        {/* Center Main Area - Run Details */}
        <main className="flex-1 flex flex-col min-w-0 overflow-y-auto bg-white rounded-xl border border-gray-200 shadow-sm">
          <div className="p-4 flex-1 flex flex-col max-w-5xl mx-auto w-full">
            
            {/* Header Info */}
            <div className="flex justify-between items-start mb-6">
              <div>
                <div className="flex items-center gap-3 mb-1">
                  <h1 className="text-xl font-bold text-gray-900">Run 00123</h1>
                  <span className="text-xs font-medium px-2 py-0.5 bg-red-100 text-red-700 rounded border border-red-200">Failed</span>
                </div>
                <div className="text-xs text-gray-500 flex gap-4">
                  <span><span className="text-gray-400">Dataset:</span> MolmoWeb-HumanSkills</span>
                  <span><span className="text-gray-400">Task:</span> Find a refundable hotel under $200</span>
                </div>
              </div>
              <div className="flex gap-2">
                <button className="p-1.5 border border-gray-200 rounded hover:bg-gray-50"><ChevronLeft size={16} /></button>
                <button className="p-1.5 border border-gray-200 rounded hover:bg-gray-50"><ChevronRight size={16} /></button>
                <button className="p-1.5 border border-gray-200 rounded hover:bg-gray-50"><SplitSquareHorizontal size={16} /></button>
              </div>
            </div>

            {/* Timeline */}
            <div className="relative mb-6 px-4">
              <div className="absolute top-1/2 left-8 right-8 h-0.5 bg-gray-200 -translate-y-1/2 z-0"></div>
              <div className="absolute top-1/2 left-8 w-[30%] h-0.5 bg-green-500 -translate-y-1/2 z-0"></div>
              <div className="relative z-10 flex justify-between">
                {[1, 2, 3, 4, 5, 6, 7, 8, 9, '...', 14].map((step, idx) => (
                  <div key={idx} className="flex flex-col items-center gap-1 bg-white">
                    <div className={`w-6 h-6 rounded-full border-2 flex items-center justify-center text-[10px] font-bold
                      ${step === 5 ? 'border-red-500 bg-white text-red-500 ring-4 ring-red-50' : 
                        (typeof step === 'number' && step < 5) ? 'border-green-500 bg-green-50 text-green-600' : 
                        'border-gray-200 bg-white text-gray-400'}`}>
                      {step}
                    </div>
                  </div>
                ))}
              </div>
              <div className="absolute left-[34%] top-8 transform -translate-x-1/2 w-0 h-0 border-l-[6px] border-l-transparent border-r-[6px] border-r-transparent border-b-[8px] border-b-red-500"></div>
            </div>

            {/* Screenshot Area */}
            <div className="border border-gray-200 rounded-lg shadow-sm bg-white overflow-hidden flex flex-col mb-4 relative">
              <div className="flex justify-between items-center px-3 py-2 border-b border-gray-100 bg-gray-50/50">
                <div className="text-xs font-medium text-gray-700">Step 5 <span className="text-gray-400 font-normal">/ 14</span> <span className="ml-2 text-gray-500 font-normal">Screenshot (after action)</span></div>
                <div className="flex items-center gap-2">
                  <button className="p-1 text-gray-400 hover:text-gray-600"><RotateCcw size={14} /></button>
                  <span className="text-xs text-gray-400 border-r border-gray-300 pr-2 mr-1">Before / After</span>
                  <button className="p-1 text-gray-400 hover:text-gray-600"><Search size={14} /></button>
                  <button className="p-1 text-gray-400 hover:text-gray-600"><RefreshCw size={14} /></button>
                  <button className="p-1 text-gray-400 hover:text-gray-600"><Maximize2 size={14} /></button>
                </div>
              </div>
              
              {/* Fake Browser Content */}
              <div className="relative bg-[#f5f5f5] h-[360px] p-2 overflow-hidden select-none">
                {/* Mock Booking.com Header */}
                <div className="bg-[#003580] text-white p-2 flex justify-between items-center rounded-t shadow-sm text-xs">
                  <div className="font-bold text-lg">Booking.com</div>
                  <div className="flex gap-4 items-center">
                    <span>USD</span>
                    <div className="flex gap-2">
                      <span className="px-2 py-1 border border-white rounded">List your property</span>
                      <span className="px-2 py-1 bg-white text-[#003580] rounded font-medium">Register</span>
                      <span className="px-2 py-1 bg-white text-[#003580] rounded font-medium">Sign in</span>
                    </div>
                  </div>
                </div>
                <div className="bg-[#003580] text-white px-2 pb-2 flex gap-4 text-xs border-b border-[#00489a]">
                   <span className="flex items-center gap-1 bg-[#00489a] px-3 py-1.5 rounded-full border border-white/20"><Search size={12}/> Stays</span>
                   <span className="flex items-center gap-1 py-1.5 opacity-80">Flights</span>
                   <span className="flex items-center gap-1 py-1.5 opacity-80">Car rentals</span>
                </div>
                
                {/* Mock Search Bar */}
                <div className="flex gap-1 p-1 bg-[#febb02] rounded mx-4 -mt-3 relative z-10 shadow">
                  <div className="flex-1 bg-white p-1.5 rounded flex items-center gap-2 text-gray-600 text-xs"><Search size={14}/> Tokyo, Japan</div>
                  <div className="flex-1 bg-white p-1.5 rounded flex items-center gap-2 text-gray-600 text-xs">Aug 12 - Aug 15</div>
                  <div className="flex-1 bg-white p-1.5 rounded flex items-center gap-2 text-gray-600 text-xs">2 adults - 1 room</div>
                  <button className="bg-[#0071c2] text-white px-4 rounded font-medium text-xs">Search</button>
                </div>

                <div className="flex mt-4 px-4 gap-4">
                  {/* Mock Sidebar Filters */}
                  <div className="w-48 hidden md:block">
                    <div className="bg-white p-3 rounded shadow-sm border border-gray-200">
                      <div className="font-bold text-xs mb-2">Filter by:</div>
                      <div className="text-[10px] font-medium mb-1">Price (per night)</div>
                      <div className="h-10 bg-blue-50 border-b-2 border-blue-500 mb-2 rounded flex items-end">
                         {/* Fake histogram */}
                         <div className="w-full flex items-end gap-0.5 px-1 opacity-50">
                            {[2,4,3,6,8,5,3,2,1,1].map((h,i)=><div key={i} className="flex-1 bg-blue-300 rounded-t" style={{height:`${h*3}px`}}></div>)}
                         </div>
                      </div>
                      <div className="flex justify-between text-[10px] text-gray-500 mb-4"><span>$0</span><span>$500+</span></div>
                      
                      <div className="text-[10px] font-medium mb-2 mt-4">Property type</div>
                      <div className="space-y-1.5">
                        {['Hotels (312)', 'Apartments (120)', 'Guesthouses (42)'].map(t=>(
                          <div key={t} className="flex items-center gap-2 text-[10px] text-gray-600">
                            <input type="checkbox" className="rounded-sm border-gray-300" /> {t}
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>

                  {/* Mock Hotel Cards */}
                  <div className="flex-1">
                    <div className="flex justify-between items-center mb-2">
                      <h2 className="text-lg font-bold">312 properties found</h2>
                      <div className="text-xs text-gray-600 border border-gray-300 rounded px-2 py-1 bg-white">Sort by: Recommended v</div>
                    </div>

                    {/* Card 1 - The Target */}
                    <div className="bg-white border-2 border-red-400 p-3 rounded shadow-sm flex gap-3 relative mb-3">
                      <img src="https://images.unsplash.com/photo-1611892440504-42a792e24d32?auto=format&fit=crop&q=80&w=200&h=150" alt="Hotel" className="w-32 h-32 object-cover rounded" />
                      <div className="flex-1 flex flex-col">
                        <div className="flex justify-between">
                          <h3 className="text-blue-600 font-bold text-sm">Shinjuku View Hotel <span className="text-yellow-400 text-[10px]">★★★★</span></h3>
                          <div className="text-right">
                             <div className="bg-[#003580] text-white text-xs font-bold p-1 rounded inline-block">8.2</div>
                             <div className="text-[10px] text-gray-500 mt-1">Very good<br/>1,234 reviews</div>
                          </div>
                        </div>
                        <div className="text-[10px] text-blue-600 underline mb-1">Shinjuku, Tokyo • Show on map</div>
                        <div className="flex gap-1 mb-2">
                          <span className="bg-green-100 text-green-800 text-[9px] px-1 rounded border border-green-200">Free cancellation</span>
                          <span className="bg-green-100 text-green-800 text-[9px] px-1 rounded border border-green-200">Pay later</span>
                        </div>
                        <div className="mt-auto flex justify-between items-end">
                          <div>
                            <div className="font-bold text-xs">Double Room</div>
                            <div className="text-[10px] text-gray-500 mb-1">2 beds (1 full, 1 sofa bed)</div>
                            <div className="text-[10px] text-red-600 font-medium">Only 1 left at this price on our site</div>
                          </div>
                          <div className="text-right">
                            <div className="font-bold text-lg">$260</div>
                            <div className="text-[9px] text-gray-500 mb-1">3 nights, 2 adults<br/>+ $41 taxes and charges</div>
                            <button className="bg-[#0071c2] text-white text-xs px-3 py-1.5 rounded font-medium">See availability &gt;</button>
                          </div>
                        </div>
                      </div>

                      {/* The Agent Click Target overlay */}
                      <div className="absolute bottom-6 right-24 flex items-center justify-center pointer-events-none">
                        <div className="w-8 h-8 rounded-full border-2 border-red-500 animate-ping absolute"></div>
                        <div className="w-4 h-4 rounded-full bg-red-500/20 border border-red-500"></div>
                        <MousePointer2 className="absolute top-2 left-2 text-gray-900 fill-white" size={20} />
                      </div>
                    </div>

                     {/* Card 2 */}
                     <div className="bg-white border border-gray-200 p-3 rounded shadow-sm flex gap-3 opacity-50">
                      <div className="w-32 h-16 bg-gray-200 rounded"></div>
                      <div className="flex-1">
                        <h3 className="text-blue-600 font-bold text-sm">Hotel Sunroute Plaza Shinjuku</h3>
                        <div className="text-[10px] text-blue-600 mb-1">Shinjuku, Tokyo</div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              {/* Playback Controls */}
              <div className="flex items-center gap-4 p-2 bg-white border-t border-gray-100">
                <div className="flex items-center gap-1">
                  <button className="p-1 text-gray-500 hover:text-gray-800"><SkipBack size={14} /></button>
                  <button className="p-1 text-gray-500 hover:text-gray-800"><Play size={14} fill="currentColor" /></button>
                  <button className="p-1 text-gray-500 hover:text-gray-800"><SkipForward size={14} /></button>
                </div>
                <div className="flex-1 flex items-center gap-2">
                  <div className="h-1.5 flex-1 bg-gray-200 rounded-full relative">
                    <div className="absolute top-0 left-0 h-full bg-red-400 rounded-full w-[35%]"></div>
                    <div className="absolute top-1/2 left-[35%] w-3 h-3 bg-red-500 rounded-full border-2 border-white -translate-y-1/2 shadow transform -translate-x-1/2"></div>
                  </div>
                </div>
                <div className="text-xs text-gray-400 w-16 text-right">Step 5 / 14</div>
                <button className="p-1 text-gray-400 hover:text-gray-600 ml-2"><Maximize2 size={14} /></button>
              </div>
            </div>

            {/* Details Section */}
            <div className="border border-gray-200 rounded-lg shadow-sm bg-white flex flex-col">
              <div className="flex border-b border-gray-200 px-2 pt-2">
                {['Action', 'Observation', 'Metadata', 'Console', 'Network'].map(tab => (
                  <button 
                    key={tab}
                    onClick={() => setActiveTab(tab)}
                    className={`px-4 py-2 text-xs font-medium border-b-2 mb-[-1px] ${
                      activeTab === tab 
                        ? 'border-indigo-600 text-indigo-600' 
                        : 'border-transparent text-gray-500 hover:text-gray-700'
                    }`}
                  >
                    {tab}
                  </button>
                ))}
              </div>
              
              <div className="p-4 space-y-4">
                {activeTab === 'Action' && (
                  <>
                    <DetailRow label="Action Type" value={<span className="bg-gray-100 px-1.5 py-0.5 rounded font-mono text-[11px] text-gray-700">click</span>} />
                    <DetailRow 
                      label="Element" 
                      value={<span className="font-mono text-[11px] text-gray-700">div.hotel-card:nth-of-type(1) &gt; a</span>} 
                      extra={<><span className="text-gray-400 ml-4 mr-2">Coordinates (x, y)</span> <span className="font-mono text-[11px]">(742, 518)</span><Copy size={12} className="ml-2 text-gray-400 cursor-pointer hover:text-gray-600"/></>}
                    />
                    <DetailRow 
                      label="Selector" 
                      value={<span className="font-mono text-[11px] text-gray-700">a[data-testid="hotel-card-1"]</span>} 
                      extra={<><span className="text-gray-400 ml-4 mr-2">Bounding Box [x, y, w, h]</span> <span className="font-mono text-[11px]">[720, 490, 320, 180]</span><Copy size={12} className="ml-2 text-gray-400 cursor-pointer hover:text-gray-600"/></>}
                    />
                    <DetailRow label="URL" value={<span className="text-blue-600 hover:underline">https://www.booking.com/searchresults.html?ss=Tokyo</span>} />
                    <DetailRow label="Timestamp" value={<span className="font-mono text-[11px] text-gray-600">2025-05-18T10:24:31.123Z</span>} />
                  </>
                )}
              </div>
            </div>

          </div>
        </main>

        {/* Right Sidebar - Eval Agent */}
        <aside className="w-80 bg-white rounded-xl border border-gray-200 flex flex-col shrink-0 shadow-sm overflow-hidden">
          <div className="p-4 border-b border-gray-200 flex justify-between items-center bg-white">
            <div className="flex items-center gap-2">
              <Sparkles size={16} className="text-indigo-600" />
              <h2 className="font-semibold text-gray-900">Eval Agent <span className="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-600 text-[10px] font-medium border border-indigo-100 ml-1">Beta</span></h2>
            </div>
            <div className="flex gap-1">
              <button className="px-2 py-1 text-xs border border-gray-200 rounded hover:bg-gray-50 font-medium bg-white shadow-sm">New Chat</button>
              <button className="p-1 text-gray-500 border border-gray-200 rounded hover:bg-gray-50 bg-white"><Settings size={14} /></button>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-4">
            
            {/* Action Buttons Grid */}
            <div className="grid grid-cols-2 gap-2">
              <button className="flex items-center gap-2 p-2 text-xs border border-gray-200 rounded-lg bg-white hover:bg-gray-50 text-gray-700 shadow-sm">
                <RefreshCw size={14} className="text-gray-400" /> Analyze this run
              </button>
              <button className="flex items-center gap-2 p-2 text-xs border border-indigo-200 rounded-lg bg-indigo-50 hover:bg-indigo-100 text-indigo-700 font-medium shadow-sm">
                <Search size={14} /> Analyze this step
              </button>
              <button className="flex items-center gap-2 p-2 text-xs border border-gray-200 rounded-lg bg-white hover:bg-gray-50 text-gray-700 shadow-sm">
                <Sparkles size={14} className="text-gray-400" /> Suggest failure label
              </button>
              <button className="flex items-center gap-2 p-2 text-xs border border-gray-200 rounded-lg bg-white hover:bg-gray-50 text-gray-700 shadow-sm">
                <Copy size={14} className="text-gray-400" /> Generate eval case
              </button>
              <button className="flex items-center gap-2 p-2 text-xs border border-gray-200 rounded-lg bg-white hover:bg-gray-50 text-gray-700 shadow-sm">
                <Search size={14} className="text-gray-400" /> Find similar failures
              </button>
              <button className="flex items-center gap-2 p-2 text-xs border border-gray-200 rounded-lg bg-white hover:bg-gray-50 text-gray-700 shadow-sm">
                <SplitSquareHorizontal size={14} className="text-gray-400" /> Compare with another run
              </button>
            </div>

            {/* Analysis Feed */}
            <div className="flex flex-col gap-4 mt-2">
              
              {/* Message Block 1 */}
              <div>
                <div className="flex justify-between items-center mb-1">
                  <span className="font-semibold text-indigo-700 text-xs">Analysis Result (Step 5)</span>
                  <span className="text-[10px] text-gray-400">10:32 AM ^</span>
                </div>
                <div className="bg-white border border-gray-200 rounded-lg p-3 shadow-sm text-xs space-y-3">
                  <p className="text-gray-800 leading-relaxed">
                    The agent clicked the first hotel card in the search results without verifying if it meets the user's constraint (price {'<'} $200 and refundable).
                  </p>
                  
                  <div>
                    <div className="font-semibold text-gray-900 mb-1.5">Findings</div>
                    <ul className="space-y-1.5">
                      <li className="flex gap-2 items-start"><AlertTriangle size={14} className="text-red-500 shrink-0 mt-0.5" /><span className="text-gray-700">Price shown is $260 ({'>'} $200 constraint)</span></li>
                      <li className="flex gap-2 items-start"><AlertTriangle size={14} className="text-red-500 shrink-0 mt-0.5" /><span className="text-gray-700">Refundability not verified before selection</span></li>
                      <li className="flex gap-2 items-start"><AlertTriangle size={14} className="text-yellow-500 shrink-0 mt-0.5" /><span className="text-gray-700">Filters on the left were not applied</span></li>
                      <li className="flex gap-2 items-start"><AlertTriangle size={14} className="text-yellow-500 shrink-0 mt-0.5" /><span className="text-gray-700">Premature selection</span></li>
                    </ul>
                  </div>

                  <div>
                    <div className="font-semibold text-gray-900 mb-1.5">Suggested Failure Label</div>
                    <div className="flex items-center gap-2">
                      <span className="px-2 py-1 bg-red-100 text-red-700 rounded text-[10px] font-medium font-mono border border-red-200">missed_constraint</span>
                      <span className="text-[10px] text-gray-500">Confidence: 0.78</span>
                    </div>
                  </div>

                  <div>
                    <div className="font-semibold text-gray-900 mb-1.5">Visual Evidence</div>
                    <div className="flex gap-2">
                      <div className="w-16 h-10 bg-gray-100 border border-gray-200 rounded overflow-hidden relative">
                         {/* Fake thumbnail 1 */}
                         <div className="absolute top-1 left-1 w-4 h-4 bg-gray-300"></div>
                         <div className="absolute top-1 right-1 w-8 h-1 bg-blue-200"></div>
                      </div>
                      <div className="w-16 h-10 bg-gray-100 border border-red-300 rounded overflow-hidden relative">
                         {/* Fake thumbnail 2 highlighted */}
                         <div className="absolute inset-1 border border-red-400"></div>
                      </div>
                      <div className="w-16 h-10 bg-gray-100 border border-gray-200 rounded flex items-center justify-center text-gray-500 text-xs">
                        +1
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              {/* Message Block 2 */}
              <div>
                <div className="flex justify-between items-center mb-1">
                  <span className="font-semibold text-indigo-700 text-xs">Eval Case Draft</span>
                  <span className="text-[10px] text-gray-400">10:32 AM ^</span>
                </div>
                <div className="bg-white border border-gray-200 rounded-lg p-3 shadow-sm text-xs space-y-2">
                  <p className="text-gray-800">The agent selected a hotel that violates the user's price constraint and did not verify refundability.</p>
                  <button className="w-full py-1.5 border border-gray-200 rounded bg-gray-50 hover:bg-gray-100 text-gray-700 font-medium">View Draft</button>
                  <div className="flex gap-2 pt-1">
                    <button className="p-1 border border-gray-200 rounded hover:bg-gray-50 text-gray-400"><ChevronLeft size={12} className="rotate-90" /></button> {/* Using as thumbs up placeholder */}
                    <button className="p-1 border border-gray-200 rounded hover:bg-gray-50 text-gray-400"><ChevronRight size={12} className="rotate-90" /></button> {/* Using as thumbs down placeholder */}
                  </div>
                </div>
              </div>

            </div>
          </div>

          <div className="p-3 bg-white border-t border-gray-200 flex flex-col gap-2">
            <div className="relative">
              <input 
                type="text" 
                placeholder="Ask about this run..." 
                className="w-full pl-3 pr-10 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:border-indigo-500 shadow-sm"
              />
              <button className="absolute right-1 top-1 bottom-1 w-8 bg-indigo-600 rounded text-white flex items-center justify-center hover:bg-indigo-700">
                <ChevronRight size={16} />
              </button>
            </div>
            <div className="text-center text-[10px] text-gray-400">
              AI can make mistakes. Please verify important information.
            </div>
          </div>
        </aside>

      </div>

      {/* Footer */}
      <footer className="px-4 py-1.5 border-t border-gray-200 bg-white text-[10px] text-gray-500 flex justify-between items-center shrink-0">
        <div className="flex gap-4">
          <span className="flex items-center gap-1">Project: <span className="text-gray-700 font-medium">Hotel Booking (MolmoWeb-HumanSkills) v</span></span>
          <span className="flex items-center gap-1"><div className="w-1.5 h-1.5 rounded-full bg-green-500"></div> Database: Local</span>
          <span className="flex items-center gap-1">Schema: <span className="text-gray-700">v1.0.0 v</span></span>
        </div>
      </footer>
    </div>
  );
};

// Helper Components
const RunCard = ({ id, status, title, date, steps, msgs }) => {
  const isFailed = status.toLowerCase() === 'failed';
  const isSuccess = status.toLowerCase() === 'success';
  
  let statusClasses = "bg-gray-100 text-gray-700";
  if (isFailed) statusClasses = "bg-red-100 text-red-700";
  if (isSuccess) statusClasses = "bg-green-100 text-green-700";
  if (status.includes('Review')) statusClasses = "bg-yellow-100 text-yellow-700";

  return (
    <div className="p-3 border border-gray-100 bg-white rounded-lg shadow-sm hover:border-indigo-200 cursor-pointer group transition-colors">
      <div className="flex justify-between items-start mb-1">
        <span className="font-semibold text-gray-900 group-hover:text-indigo-600">Run {id}</span>
        <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${statusClasses}`}>{status}</span>
      </div>
      <p className="text-xs text-gray-600 mb-3 truncate">{title}</p>
      <div className="flex justify-between items-center text-[10px] text-gray-500">
        <span>{date}</span>
        <div className="flex gap-3">
          <span>{steps} steps</span>
          <span className="flex items-center gap-1"><MessageSquare size={10} /> {msgs}</span>
        </div>
      </div>
    </div>
  );
};

const DetailRow = ({ label, value, extra }) => (
  <div className="flex items-start">
    <div className="w-24 text-gray-500 font-medium shrink-0 pt-0.5">{label}</div>
    <div className="flex-1 flex flex-wrap items-center">
      {value}
      {extra}
    </div>
  </div>
);

export default App;